#!/usr/bin/env python3
"""Scan and repair incomplete post-canonical derivation coverage."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


async def _scan_source(conn, source_id: str, *, apply: bool) -> dict:
    from topos.enrichment.catalog import all_jobs_for_source, coverage_table_for_job, get_catalog_entry
    from topos.features.timeline_projection import timeline_coverage_for_source
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal, run_post_canonical_pipeline
    from topos.pipeline.job_store import enqueue_job
    from topos.sources.registry import REGISTRY

    source_def = REGISTRY.get(source_id)
    if source_def is None:
        return {"source_id": source_id, "status": "skipped", "reason": "unknown source"}

    gaps: dict[str, int] = {}
    for job_id in all_jobs_for_source(source_def):
        if job_id == "timeline":
            stats = timeline_coverage_for_source(conn, source_id)
            missing = int(stats.get("missing_records") or 0)
        else:
            table = coverage_table_for_job(job_id)
            missing = 0
            if table:
                try:
                    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                    id_col = "message_id" if "message_id" in cols else "record_id"
                    if "source_id" in cols and id_col in cols:
                        total = conn.execute(
                            f"SELECT COUNT(DISTINCT {id_col}) FROM {table} WHERE source_id=?",
                            (source_id,),
                        ).fetchone()
                        canonical = load_canonical_records_for_signal(conn, source_def)
                        enriched = int(total[0]) if total else 0
                        missing = max(0, len(canonical) - enriched)
                except Exception:
                    missing = 0
        if missing:
            gaps[job_id] = missing

    result = {"source_id": source_id, "gaps": gaps, "applied": False}
    if not gaps or not apply:
        return result

    records = load_canonical_records_for_signal(conn, source_def)
    if not records:
        return result

    if "timeline" in gaps:
        from topos.features.timeline_projection import repair_timeline_for_source

        repair_timeline_for_source(conn, source_id, missing_only=True, dry_run=False)

    fast_jobs = [job for job in gaps if job != "timeline"]
    if fast_jobs:
        await run_post_canonical_pipeline(
            source_def=source_def,
            canonical_records=records,
            sync_batch_id=f"repair-derivation-{uuid.uuid4().hex[:8]}",
            job_names=fast_jobs,
            run_enrichment=True,
            force_signal=True,
        )

    enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={"source_id": source_id, "recover": True},
        source_id=source_id,
        idempotency_key=f"repair_derivation:{source_id}",
    )
    result["applied"] = True
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument("--sources", nargs="*", default=None)
    parser.add_argument("--write-id", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    os.environ["TOPOS_DATABASE_PATH"] = args.db
    from topos.storage.adapters.factory import AdapterFactory

    AdapterFactory.create("local_database", db_path=Path(args.db))
    from topos.core.state import get_db_connection
    from topos.sources.registry import REGISTRY

    conn = get_db_connection()
    if conn is None:
        raise SystemExit("Database connection unavailable")

    source_ids = args.sources or list(REGISTRY.keys())
    report = []
    for sid in source_ids:
        report.append(await _scan_source(conn, sid, apply=args.apply))

    print(json.dumps({"dry_run": not args.apply, "sources": report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
