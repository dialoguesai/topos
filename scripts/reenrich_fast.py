"""Re-run the FAST (non-LLM) signal-derivation jobs over existing canonical rows so the
derived layers adopt code changes that only take effect at ingest time (embeddable_content
expansion → index contacts/places/identifiers into signal_embeddings; entity-spine hygiene;
stat families).

Uses the MODERN path (run_post_canonical_pipeline → SignalDerivationOrchestrator), which
writes signal_embeddings + the sqlite-vec ANN table correctly. Restricts to fast jobs and
skips the slow Ollama LLM jobs (dimension_summary/topic_clusters — gemma4:12b is minutes per
message, and those layers were measured non-bottlenecks). Per source, so scope + provenance
stay correct. Embeddings dedup by content_hash: unchanged rows are no-ops; only newly-
embeddable rows (contacts/places) actually embed.

Source coverage is three populations, not just the static registry (iterating REGISTRY
alone silently skipped every app-ingest lane — the 2026-07-27 facts backfill processed
1,234 registry rows and 0 of the grow_journal rows):
  1. static REGISTRY sources, on their own canonical lane;
  2. runtime-installed sources (source_runtime_installs, e.g. grow_journal) — their
     definitions only exist as persisted JSON, nothing repopulates REGISTRY here;
  3. orphan source_ids found in canonical tables with no surviving definition at all
     (hand-injected rows, retired installs), via a synthesized minimal definition.
A source whose rows live in a canonical table outside its definition's lane (declared-
mapping writes, e.g. github_activity rows in journal_entries) gets one work item per
(source, table) pair so every row is loaded.

  .venv/bin/python scripts/reenrich_fast.py --sources demo_contacts_file   # test one source
  .venv/bin/python scripts/reenrich_fast.py                                 # all sources with rows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

FAST_JOBS = ["embeddings", "entities", "statistics", "facts", "timeline"]

# canonical table → lane group understood by load_canonical_records_for_signal.
# documents is deliberately absent: owner-only leaf lane, never signal-derived.
TABLE_GROUPS = {
    "activity_events": "activity",
    "conversation_messages": "conversations",
    "calendar_events": "schedule",
    "journal_entries": "journal",
    "profile_records": "profile",
    "financial_transactions": "financial",
    "location_events": "places",
    "contacts": "contacts",
    "ai_chat_messages": "ai_messages",
}


def _runtime_installed_defs(conn) -> dict:
    """Active runtime-install definitions by source_id, newest row wins.

    Same status filter as sources.registry._runtime_installed_sources_by_scope;
    read directly (instead of rehydrate_active_installs_runtime) so a reenrich
    never mutates install rows or runtime parser/mapper registries.
    """
    from topos.sources.definitions import definition_from_payload

    out: dict = {}
    try:
        rows = conn.execute(
            """SELECT source_id, source_definition_json FROM source_runtime_installs
               WHERE is_active=1 AND status IN ('installed', 'active', 'ready')
               ORDER BY rowid DESC"""
        ).fetchall()
    except Exception:
        return out
    for source_id, def_json in rows:
        sid = str(source_id or "").strip()
        if not sid or sid in out:
            continue
        try:
            payload = json.loads(def_json) if isinstance(def_json, str) else (def_json or {})
            if isinstance(payload, dict) and payload:
                out[sid] = definition_from_payload(payload)
        except Exception:
            continue
    return out


def _with_group(source_def, group: str):
    """Copy of source_def pointed at another canonical lane so
    load_canonical_records_for_signal reads the right table."""
    if getattr(source_def, "canonical_group_id", None) == group:
        return source_def
    from topos.sources.definitions import definition_from_payload

    payload = source_def.to_dict()
    payload["canonical_group_id"] = group
    return definition_from_payload(payload)


def _synthetic_def(source_id: str, group: str):
    """Minimal definition for rows whose source has no surviving definition."""
    from topos.sources.definitions import definition_from_payload

    return definition_from_payload(
        {
            "source_id": source_id,
            "display_name": source_id,
            "source_type": "ui_stream",
            "schema_id": "",
            "parser_id": "",
            "canonical_group_id": group,
        }
    )


def collect_work_items(conn, registry: dict, requested=None) -> list:
    """(source_id, source_def, group) work items covering every canonical row.

    Pass 1: registry + runtime-install definitions on their own lane (registry
    wins a source_id collision — the bundled definition is authoritative).
    Pass 2: every remaining (canonical table, source_id) pair — app-ingest
    lanes, declared-mapping writes into a sibling table, and orphans.
    """
    runtime_defs = _runtime_installed_defs(conn)
    work: list = []
    seen: set = set()

    for sid, sdef in {**runtime_defs, **registry}.items():
        if requested is not None and sid not in requested:
            continue
        group = str(getattr(sdef, "canonical_group_id", "") or "")
        if not group:
            continue  # non-canonical source; pass 2 picks up any rows it has
        work.append((sid, sdef, group))
        seen.add((sid, group))

    for table, group in TABLE_GROUPS.items():
        try:
            rows = conn.execute(f"SELECT DISTINCT source_id FROM {table}").fetchall()
        except Exception:
            continue  # table absent on older DBs
        for (raw_sid,) in rows:
            sid = str(raw_sid or "").strip()
            if not sid or (sid, group) in seen:
                continue
            if requested is not None and sid not in requested:
                continue
            seen.add((sid, group))
            base = registry.get(sid) or runtime_defs.get(sid)
            sdef = _with_group(base, group) if base is not None else _synthetic_def(sid, group)
            work.append((sid, sdef, group))
    return work


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".topos/database.db"))
    ap.add_argument(
        "--sources",
        nargs="*",
        default=None,
        help="source_ids; default = every source with canonical rows (registry, runtime installs, orphans)",
    )
    ap.add_argument("--jobs", nargs="*", default=FAST_JOBS)
    ap.add_argument("--limit", type=int, default=5000, help="max rows loaded per (source, table) lane")
    args = ap.parse_args()

    os.environ["TOPOS_DATABASE_PATH"] = args.db
    from topos.storage.adapters.factory import AdapterFactory
    AdapterFactory.create("local_database", db_path=Path(args.db))
    from topos.core.state import get_db_connection
    from topos.sources.registry import REGISTRY
    from topos.ingestion.canonical_pipeline import (
        load_canonical_records_for_signal,
        run_post_canonical_pipeline,
    )

    conn = get_db_connection()
    requested = set(args.sources) if args.sources else None
    grand: dict = {}
    for sid, source_def, group in collect_work_items(conn, REGISTRY, requested):
        records = load_canonical_records_for_signal(conn, source_def, limit=args.limit)
        if not records:
            continue
        out = await run_post_canonical_pipeline(
            source_def=source_def,
            canonical_records=records,
            sync_batch_id=f"reenrich-{uuid.uuid4().hex[:8]}",
            job_names=args.jobs,
            run_enrichment=True,
            force_signal=True,
        )
        derive = out.get("signal_derivation") or {}
        created = derive.get("records_created") or {}
        print(f"{sid}[{group}]: {len(records)} rows → jobs={derive.get('jobs_run')} created={created}")
        for k, v in created.items():
            grand[k] = grand.get(k, 0) + (v or 0)
    print(f"\nTOTAL created: {grand}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
