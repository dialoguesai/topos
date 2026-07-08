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

  .venv/bin/python scripts/reenrich_fast.py --sources demo_contacts_file   # test one source
  .venv/bin/python scripts/reenrich_fast.py                                 # all active sources
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path

FAST_JOBS = ["embeddings", "entities", "statistics", "facts", "timeline"]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".topos/database.db"))
    ap.add_argument("--sources", nargs="*", default=None, help="source_ids; default = all in registry with canonical rows")
    ap.add_argument("--jobs", nargs="*", default=FAST_JOBS)
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
    source_ids = args.sources or list(REGISTRY.keys())
    grand: dict = {}
    for sid in source_ids:
        source_def = REGISTRY.get(sid)
        if source_def is None:
            continue
        records = load_canonical_records_for_signal(conn, source_def)
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
        print(f"{sid}: {len(records)} rows → jobs={derive.get('jobs_run')} created={created}")
        for k, v in created.items():
            grand[k] = grand.get(k, 0) + (v or 0)
    print(f"\nTOTAL created: {grand}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
