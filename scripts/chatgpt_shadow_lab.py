#!/usr/bin/env python3
"""Ingest a ChatGPT export into a throwaway shadow database and snapshot its graph.

Nothing here touches the live node. The shadow database is created fresh, the
export is read with the same reader the product uses, the same signal-derivation
lane runs over it, and the resulting knowledge graph is written out as one JSON
snapshot that a viewer can hold in memory.

The point is comparison. Run it once now (``--label before``), close the gaps in
PLAN_CHATGPT_IMPORT.md Sprint 4, run it again (``--label after``), and diff the
two snapshots: same export, same window, different density.

    python scripts/chatgpt_shadow_lab.py --input ../conversations.json --label before
    python scripts/chatgpt_shadow_lab.py --input ~/Downloads/chatgpt-export \\
        --label after --months 6

The snapshot holds the owner's own material — entity names, fact text,
conversation titles. It is written outside the repo on purpose; do not commit it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE_ID = "chatgpt_file_ingestion"
SCHEMA_ID = "chatgpt.conversation.v2"
CONVERSATIONS_FILENAME = "conversations.json"

# Row caps per table. High enough to be the whole picture for a personal export,
# low enough that the snapshot stays loadable in a browser. Every cap that bites
# is reported in the snapshot rather than silently trimming.
CAPS = {
    "entities": 6000,
    "entity_edges": 12000,
    "entity_mentions": 20000,
    "facts": 6000,
    "objects": 6000,
    "graph_nodes": 6000,
    "graph_edges": 12000,
    "topics": 4000,
}


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


def resolve_export(path: Path) -> Path:
    """A folder, a .zip, or the JSON itself → a path to conversations.json."""
    if path.is_dir():
        candidate = path / CONVERSATIONS_FILENAME
        if candidate.is_file():
            return candidate
        matches = sorted(path.rglob(CONVERSATIONS_FILENAME), key=lambda p: len(p.parts))
        if not matches:
            raise SystemExit(f"No {CONVERSATIONS_FILENAME} under {path}")
        return matches[0]
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == CONVERSATIONS_FILENAME]
            if not names:
                raise SystemExit(f"No {CONVERSATIONS_FILENAME} inside {path}")
            member = min(names, key=lambda n: n.count("/"))
            target = Path(os.environ.get("TMPDIR", "/tmp")) / f"chatgpt-shadow-{os.getpid()}.json"
            with archive.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            return target
    return path


def slice_export(path: Path, limit: Optional[int]) -> tuple[Path, int, int]:
    """Return (path_to_use, conversations_in_file, conversations_ingested)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raw = [raw]
    total = len(raw)
    if not limit or limit >= total:
        return path, total, total
    target = Path(os.environ.get("TMPDIR", "/tmp")) / f"chatgpt-shadow-slice-{os.getpid()}.json"
    target.write_text(json.dumps(raw[:limit]), encoding="utf-8")
    return target, total, limit


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


def configure_runtime(db_path: Path, ingest_root: Path) -> None:
    """Point the engine at the shadow database *before* anything imports it.

    ``TOPOS_DATABASE_PATH`` is read at import time in several places, so this has
    to run before the first ``topos.*`` import or the run lands on the live node.
    """
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ["TOPOS_DATABASE_MODE"] = "local"
    os.environ["TOPOS_INGESTION_BASE_PATH"] = str(ingest_root)
    os.environ.setdefault("TOPOS_KEY", "chatgpt-shadow-lab")
    os.environ.setdefault("CONTROL_PLANE_URL", "")
    os.environ.setdefault("TOPOS_CONTROL_PLANE_URL", "")
    for module in [name for name in list(sys.modules) if name.startswith("topos.")]:
        sys.modules.pop(module, None)


def _write_job_overrides(disabled: List[str]) -> None:
    """Switch jobs off for this source through the runtime override the product
    already has (``source_enrichment_overrides``), rather than editing a
    definition — so nothing about the source itself differs between runs."""
    from topos.core.state import get_db_connection, set_engine_config_value
    from topos.enrichment.source_overrides import ENGINE_CONFIG_KEY_SOURCE_ENRICHMENTS

    conn = get_db_connection()
    if conn is None:
        return
    payload = {
        SOURCE_ID: {job: {"enabled": False, "lanes": ["canonical", "signal"]} for job in disabled}
    }
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_SOURCE_ENRICHMENTS, json.dumps(payload))


def assert_not_live(db_path: Path) -> None:
    live = Path(os.path.expanduser("~/.topos/database.db")).resolve()
    if db_path.resolve() == live:
        raise SystemExit("Refusing to run: --db points at the live node database.")


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, cap: int) -> tuple[List[Dict[str, Any]], int]:
    """(rows, total). The cap is reported, never silently applied."""
    table = sql.split(" FROM ")[1].split()[0]
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return [], 0
    try:
        cursor = conn.execute(f"{sql} LIMIT {cap}")
    except sqlite3.Error:
        return [], total
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()], int(total)


def _json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _fact_text(payload: Any) -> str:
    """One readable line out of a derived record, whatever shape it took.

    ``signal_facts`` is not a table of beliefs — it is every derived record the
    lane writes, and each producer uses its own payload shape. These are the
    shapes the ChatGPT lane actually produces, checked against a real run rather
    than guessed.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""

    # An extracted entity: the surface and what it was taken to be.
    surface = payload.get("entity_text") or payload.get("canonical_name")
    if isinstance(surface, str) and surface.strip():
        kind = payload.get("entity_type")
        return f"{surface.strip()} ({kind})" if isinstance(kind, str) and kind else surface.strip()

    # A topic cluster: its label, and the terms that earned the label.
    tag = payload.get("tag") or payload.get("label")
    if isinstance(tag, str) and tag.strip():
        terms = payload.get("label_terms")
        if isinstance(terms, list) and terms:
            joined = ", ".join(str(t) for t in terms[:5] if t)
            members = payload.get("member_count")
            suffix = f" · {members} members" if isinstance(members, int) else ""
            return f"{tag.strip()} — {joined}{suffix}"
        return tag.strip()

    for key in ("summary_text", "text", "statement", "fact", "summary", "description", "title"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    subject = payload.get("subject") or payload.get("entity") or payload.get("head")
    predicate = payload.get("predicate") or payload.get("relation")
    obj = payload.get("object") or payload.get("value") or payload.get("tail")
    parts = [str(p).strip() for p in (subject, predicate, obj) if isinstance(p, (str, int, float)) and str(p).strip()]
    if len(parts) >= 2:
        return " · ".join(parts)
    return json.dumps({k: v for k, v in payload.items() if k not in ("provenance", "sync_batch_id")})[:200]


def _fact_kind(payload: Any) -> str:
    """What sort of derived record this is, for grouping in the viewer."""
    if not isinstance(payload, dict):
        return "unknown"
    for key in ("object_type", "job_id", "kind"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if payload.get("entity_text"):
        return "entity"
    return "record"


def snapshot(conn: sqlite3.Connection, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the derivation lane produced, in one loadable object."""
    conn.row_factory = None

    entities, entities_total = _rows(
        conn,
        "SELECT entity_id, entity_type, canonical_name, aliases_json, is_self, contact_id,"
        " first_seen, last_seen, mention_count, metadata_json FROM entities"
        " ORDER BY mention_count DESC, canonical_name",
        CAPS["entities"],
    )
    for row in entities:
        row["aliases"] = _json(row.pop("aliases_json")) or []
        row["metadata"] = _json(row.pop("metadata_json")) or {}

    edges, edges_total = _rows(
        conn,
        "SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,"
        " last_event_at, valid_from, valid_to, metadata_json FROM entity_edges"
        " ORDER BY evidence_count DESC, weight DESC",
        CAPS["entity_edges"],
    )
    for row in edges:
        row["metadata"] = _json(row.pop("metadata_json")) or {}

    mentions, mentions_total = _rows(
        conn,
        "SELECT mention_id, entity_id, record_id, source_id, canonical_table, surface_text,"
        " confidence, event_at, authored_by_owner FROM entity_mentions ORDER BY event_at DESC",
        CAPS["entity_mentions"],
    )

    facts, facts_total = _rows(
        conn,
        "SELECT fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at"
        " FROM signal_facts ORDER BY created_at DESC",
        CAPS["facts"],
    )
    for row in facts:
        payload = _json(row.pop("payload_json"))
        row["payload"] = payload
        row["text"] = _fact_text(payload)
        row["kind"] = _fact_kind(payload)
        # The columns are often null while the payload names its producer;
        # showing "rules" for a transformer's output would misreport provenance.
        if isinstance(payload, dict):
            row["model"] = row.get("model") or payload.get("model")
            row["provider"] = row.get("provider") or payload.get("provider")

    objects, objects_total = _rows(
        conn,
        "SELECT object_id, signal_dimension, object_type, object_key, payload_json, confidence,"
        " period_start, period_end, extractor_version FROM signal_objects ORDER BY confidence DESC",
        CAPS["objects"],
    )
    for row in objects:
        payload = _json(row.pop("payload_json"))
        row["payload"] = payload
        row["text"] = _fact_text(payload)

    graph_nodes, graph_nodes_total = _rows(
        conn, "SELECT node_id, node_type, label, source_id, metadata_json FROM graph_nodes", CAPS["graph_nodes"]
    )
    for row in graph_nodes:
        row["metadata"] = _json(row.pop("metadata_json")) or {}
    graph_edges, graph_edges_total = _rows(
        conn,
        "SELECT edge_id, src_node_id, dst_node_id, edge_type, weight, source_id FROM graph_edges"
        " ORDER BY weight DESC",
        CAPS["graph_edges"],
    )

    topics, topics_total = _rows(
        conn,
        "SELECT topic_id, record_id, source_id, topic, model, payload_json FROM message_topics",
        CAPS["topics"],
    )
    for row in topics:
        row["payload"] = _json(row.pop("payload_json"))

    clusters, clusters_total = _rows(
        conn,
        "SELECT cluster_id, label, dimension, member_count, label_terms_json FROM topic_clusters"
        " ORDER BY member_count DESC",
        500,
    )
    for row in clusters:
        row["label_terms"] = _json(row.pop("label_terms_json")) or []

    conversations, conversations_total = _rows(
        conn,
        "SELECT conversation_id, title, created_at, updated_at, source_id FROM ai_chat_conversations"
        " ORDER BY updated_at DESC",
        4000,
    )
    turn_counts = {
        row[0]: {"human": row[1], "assistant": row[2], "chars": row[3]}
        for row in conn.execute(
            "SELECT conversation_id, SUM(sender_type='human'), SUM(sender_type='assistant'),"
            " SUM(LENGTH(content)) FROM ai_chat_messages GROUP BY conversation_id"
        )
    }
    for row in conversations:
        row.update(turn_counts.get(row["conversation_id"], {"human": 0, "assistant": 0, "chars": 0}))

    def scalar(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            return 0

    counts = {
        "conversations": conversations_total,
        "messages": scalar("SELECT COUNT(*) FROM ai_chat_messages"),
        "owner_turns": scalar("SELECT COUNT(*) FROM ai_chat_messages WHERE sender_type='human'"),
        "assistant_turns": scalar("SELECT COUNT(*) FROM ai_chat_messages WHERE sender_type='assistant'"),
        "empty_messages": scalar("SELECT COUNT(*) FROM ai_chat_messages WHERE TRIM(COALESCE(content,''))=''"),
        "entities": entities_total,
        "entity_edges": edges_total,
        "entity_mentions": mentions_total,
        "facts": facts_total,
        "signal_objects": objects_total,
        "graph_nodes": graph_nodes_total,
        "graph_edges": graph_edges_total,
        "message_topics": topics_total,
        "topic_clusters": clusters_total,
        "embeddings": scalar("SELECT COUNT(*) FROM signal_embeddings"),
        "message_entities": scalar("SELECT COUNT(*) FROM message_entities"),
        "relationship_edges": scalar("SELECT COUNT(*) FROM relationship_edges"),
    }

    capped = {
        name: total
        for name, total, cap in (
            ("entities", entities_total, CAPS["entities"]),
            ("entity_edges", edges_total, CAPS["entity_edges"]),
            ("entity_mentions", mentions_total, CAPS["entity_mentions"]),
            ("facts", facts_total, CAPS["facts"]),
            ("signal_objects", objects_total, CAPS["objects"]),
            ("graph_nodes", graph_nodes_total, CAPS["graph_nodes"]),
            ("graph_edges", graph_edges_total, CAPS["graph_edges"]),
            ("message_topics", topics_total, CAPS["topics"]),
        )
        if total > cap
    }

    return {
        "meta": {**meta, "capped_tables": capped},
        "counts": counts,
        "conversations": conversations,
        "entities": entities,
        "entity_edges": edges,
        "entity_mentions": mentions,
        "facts": facts,
        "signal_objects": objects,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "message_topics": topics,
        "topic_clusters": clusters,
    }


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


async def snapshot_existing(args: argparse.Namespace) -> Dict[str, Any]:
    """Re-read a shadow database that was already built. Used when a run is
    interrupted, and to re-pack a snapshot after the viewer changes."""
    db_path = Path(args.db).expanduser()
    assert_not_live(db_path)
    if not db_path.exists():
        raise SystemExit(f"No shadow database at {db_path}")
    configure_runtime(db_path, db_path.parent / f"{db_path.stem}-ingest")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        declared = declared_coverage_for(conn)
        meta = {
            "label": args.label,
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
            "export_path": str(args.input),
            "conversations_in_file": None,
            "conversations_ingested": None,
            "database": str(db_path),
            "source_id": SOURCE_ID,
            "schema_id": SCHEMA_ID,
            "ingest_options": {},
            "reader_ledger": {},
            "declared_coverage": declared,
            "ingest_result": {"status": "snapshot-only"},
            "ingest_seconds": None,
            "derivation_seconds": None,
            "extractor": "chatgpt_export.v3",
            "facts_llm": args.facts_llm,
            "snapshot_only": True,
        }
        return snapshot(conn, meta)
    finally:
        conn.close()


def declared_coverage_for(conn: sqlite3.Connection) -> Dict[str, Any]:
    """What the declared lane would mint from what is stored."""
    try:
        from topos.features.entities.chatgpt_declared import coverage

        rows = conn.execute("SELECT metadata_json, source_id FROM ai_chat_messages").fetchall()
        return coverage([{"metadata_json": r[0], "source_id": r[1]} for r in rows])
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    export_path = resolve_export(Path(args.input).expanduser())
    ingest_path, conversations_in_file, conversations_ingested = slice_export(
        export_path, args.max_conversations
    )

    db_path = Path(args.db).expanduser()
    assert_not_live(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    ingest_root = db_path.parent / f"{db_path.stem}-ingest"
    ingest_root.mkdir(parents=True, exist_ok=True)
    if args.declared_lane == "off":
        os.environ["TOPOS_DECLARED_PRODUCERS"] = "off"
    if args.facts_llm != "auto":
        # Tri-state env read by features/facts/llm_extract.facts_llm_enabled.
        os.environ["TOPOS_FACTS_LLM"] = "1" if args.facts_llm == "on" else "0"
    configure_runtime(db_path, ingest_root)

    disabled = [j.strip() for j in str(args.disable_jobs or "").split(",") if j.strip()]
    if disabled:
        _write_job_overrides(disabled)

    date_from = args.date_from
    if date_from is None and args.months:
        date_from = (datetime.now(tz=timezone.utc) - timedelta(days=args.months * 30.44)).isoformat()
    ingest_options = {
        "date_from": date_from,
        "date_to": args.date_to,
        "include_alternate_branches": args.include_alternate_branches,
        "include_tool_output": args.include_tool_output,
    }

    # Reader-level ledger first: what the window kept, before anything is stored.
    from topos.ingestion.parsers.chatgpt_export import DropLedger, ExportOptions, iter_export

    ledger = DropLedger()
    payload = json.loads(ingest_path.read_text(encoding="utf-8"))
    for _ in iter_export(payload, ExportOptions.from_payload(ingest_options), ledger):
        pass
    reader_ledger = ledger.as_dict()

    from topos.ingestion.ingest_helpers import ingest_file_payload

    started = time.monotonic()
    ingest_result = await ingest_file_payload(
        dataset_id=args.dataset_id,
        schema_id=SCHEMA_ID,
        file_path=str(ingest_path),
        file_format="json",
        source_id=SOURCE_ID,
        ingest_options=ingest_options,
    )
    ingest_seconds = round(time.monotonic() - started, 1)

    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        raise SystemExit("database connection unavailable after ingest")

    # The manager runs the signal lane itself when the source's enrichment
    # trigger is automatic (run_post_canonical_pipeline), so an unconditional
    # second pass here re-ran NER and embeddings over the whole corpus — roughly
    # doubling a run that already takes tens of minutes.
    already_derived = _derivation_present(conn)
    derive_seconds = 0.0
    derivation_source = "ingest" if already_derived else "none"
    if not args.skip_derivation and not already_derived:
        started = time.monotonic()
        await _derive(conn, args)
        derive_seconds = round(time.monotonic() - started, 1)
        derivation_source = "explicit"

    declared_coverage = declared_coverage_for(conn)

    meta = {
        "label": args.label,
        "declared_coverage": declared_coverage,
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        "export_path": str(export_path),
        "conversations_in_file": conversations_in_file,
        "conversations_ingested": conversations_ingested,
        "database": str(db_path),
        "source_id": SOURCE_ID,
        "schema_id": SCHEMA_ID,
        "ingest_options": ingest_options,
        "reader_ledger": reader_ledger,
        "ingest_result": {
            k: v for k, v in ingest_result.items() if k in ("status", "records_processed", "errors_count")
        },
        "ingest_seconds": ingest_seconds,
        "derivation_seconds": derive_seconds,
        "derivation_source": derivation_source,
        "extractor": "chatgpt_export.v3",
        "facts_llm": args.facts_llm,
        "declared_lane": args.declared_lane,
        "disabled_jobs": disabled,
    }
    return snapshot(conn, meta)


def _derivation_present(conn: sqlite3.Connection) -> bool:
    """Did the ingest already produce signal output for this corpus?"""
    for table in ("entities", "signal_embeddings", "message_entities"):
        try:
            if int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0) > 0:
                return True
        except sqlite3.Error:
            continue
    return False


async def _derive(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Run the signal-derivation lane over everything the ingest wrote."""
    from topos.enrichment.orchestrator import SignalDerivationOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    bundle = AdapterFactory.create("local_database", conn=conn)
    rows = conn.execute(
        "SELECT message_id, conversation_id, sender_type, content, source_id, event_at, metadata_json"
        " FROM ai_chat_messages ORDER BY event_at"
    ).fetchall()
    messages = [
        {
            "message_id": r[0],
            "conversation_id": r[1],
            "thread_id": r[1],
            "sender_type": r[2],
            "content": r[3],
            "source_id": r[4] or SOURCE_ID,
            "event_at": r[5],
            # The declared lane reads its facets off here; without it the
            # derivation pass sees none of what the export declared.
            "metadata_json": r[6],
            "_table": "ai_chat_messages",
        }
        for r in rows
    ]
    if not messages:
        return
    def progress(done: int, total: int, job: str, *_rest: Any) -> None:
        if total and (done % 25 == 0 or done == total):
            print(f"derive {done}/{total} · {job}", flush=True)

    orchestrator = SignalDerivationOrchestrator(adapters=bundle)
    print(f"derive start · {len(messages)} turns", flush=True)
    await orchestrator.run_signal_derivation(
        messages,
        source_id=SOURCE_ID,
        sync_batch_id=f"shadow-{args.label}",
        progress_callback=progress,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="export folder, .zip, or conversations.json")
    parser.add_argument("--label", default="before", help="run label stamped into the snapshot")
    parser.add_argument("--db", default=None, help="shadow database path (never the live node)")
    parser.add_argument("--snapshot", default=None, help="where to write the JSON snapshot")
    parser.add_argument("--dataset-id", default="shadow:chatgpt")
    parser.add_argument("--months", type=float, default=None, help="only conversations active in the last N months")
    parser.add_argument("--date-from", default=None)
    parser.add_argument("--date-to", default=None)
    parser.add_argument("--max-conversations", type=int, default=None)
    parser.add_argument("--include-alternate-branches", action="store_true")
    parser.add_argument("--include-tool-output", action="store_true")
    parser.add_argument("--reset", action="store_true", help="delete the shadow database first")
    parser.add_argument("--skip-derivation", action="store_true", help="ingest only, no signal lane")
    parser.add_argument(
        "--disable-jobs",
        default="",
        help=(
            "comma-separated enrichment jobs to switch off for this run. 'topics' and the "
            "signal 'facts' lane each call a local LLM once per turn (~40s), which on a "
            "full export is hours; excluding them makes a run finishable and, applied to "
            "both sides, keeps a before/after comparison fair."
        ),
    )
    parser.add_argument(
        "--declared-lane",
        choices=("on", "off"),
        default="on",
        help="the Sprint 4 declared minting lane; 'off' reproduces the pre-Sprint-4 graph",
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="snapshot an existing shadow database without ingesting anything",
    )
    parser.add_argument(
        "--facts-llm",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "LLM fact extraction. 'auto' uses the node's own setting (a local model, roughly "
            "40s per turn on this hardware); 'off' leaves only the rules floor, which is much "
            "faster and understates facts. Stamped into the snapshot either way."
        ),
    )
    args = parser.parse_args()

    if args.db is None:
        args.db = f"~/.topos/shadow/chatgpt-{args.label}.db"
    if args.snapshot is None:
        args.snapshot = f"~/.topos/shadow/chatgpt-{args.label}-snapshot.json"

    result = asyncio.run(snapshot_existing(args) if args.snapshot_only else run(args))

    out = Path(args.snapshot).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result), encoding="utf-8")

    counts = result["counts"]
    meta = result["meta"]
    print(f"label            {meta['label']}")
    print(f"database         {meta['database']}")
    print(f"snapshot         {out}  ({out.stat().st_size / 1024**2:.1f} MB)")
    print(f"conversations    {counts['conversations']} of {meta.get('conversations_in_file')} in the file")
    print(f"turns            {counts['messages']}  (you {counts['owner_turns']} / assistant {counts['assistant_turns']})")
    print(f"empty rows       {counts['empty_messages']}")
    print(f"entities         {counts['entities']}")
    print(f"entity edges     {counts['entity_edges']}")
    print(f"mentions         {counts['entity_mentions']}")
    print(f"facts            {counts['facts']}")
    print(f"signal objects   {counts['signal_objects']}")
    print(f"graph            {counts['graph_nodes']} nodes / {counts['graph_edges']} edges")
    print(f"topics/clusters  {counts['message_topics']} / {counts['topic_clusters']}")
    print(f"embeddings       {counts['embeddings']}")
    print(f"timing           ingest {meta.get('ingest_seconds')}s · derivation {meta.get('derivation_seconds')}s"
          f" ({meta.get('derivation_source', '?')})")
    print(f"declared lane    {meta.get('declared_coverage')}")
    if meta["capped_tables"]:
        print(f"capped           {meta['capped_tables']} (snapshot truncated, counts are full)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
