"""Keep the derived-object index current after every enrichment batch.

The derivation layer writes ``signal_objects``; retrieval reads
``signal_embeddings``. ``features/signal/derived_index`` renders one into the
other, and this job is what makes that happen on its own — without it the index
would be a one-shot backfill, and a one-shot step that a recurring producer
overwrites is a fix that quietly stops being true (the withdrawal-step failure
mode: the ladder runs once, the producer runs forever).

The pass is incremental by content hash, so the steady-state cost is a scan of
the active objects and no inference at all — measured at ~50ms against 490
objects on the first live node. That is cheap enough to run every batch, which
is what makes "new objects are embedded on write" true rather than aspirational.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection

logger = logging.getLogger("topos.enrichment.jobs.derived_object_embeddings")


class DerivedObjectEmbeddingsJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return "signal_embeddings"

    def get_job_name(self) -> str:
        return "derived_object_embeddings"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        # Runs on an empty batch too: the objects this indexes are produced by
        # SIBLING jobs over the same batch (entities -> dossiers, derivation ->
        # facts, extraction -> edges), so "this batch had no messages" says
        # nothing about whether there is anything new to index.
        return True

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        def _index() -> Optional[Dict[str, int]]:
            # Embedding is a blocking model call and the upsert takes the write
            # gate — both belong on a worker thread with its own connection,
            # never on the event loop.
            from ....features.signal.derived_index import index_derived_objects

            conn = get_db_connection()
            if conn is None:
                return None
            return index_derived_objects(conn)

        try:
            counts = await asyncio.to_thread(_index)
        except Exception as exc:  # noqa: BLE001 — indexing must never fail a batch
            logger.warning("derived-object indexing skipped: %s", exc)
            counts = None

        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        if counts is None:
            return [{"_deferred": True, "error": "database_unavailable"}]
        if counts.get("written") or counts.get("pruned"):
            logger.info(
                "[PIPELINE:DERIVED_INDEX] rendered=%d written=%d pruned=%d unchanged=%d skipped=%d",
                counts.get("rendered", 0),
                counts.get("written", 0),
                counts.get("pruned", 0),
                counts.get("unchanged", 0),
                counts.get("skipped_unrenderable", 0),
            )
        # Rows are written straight through the vector adapter (same store the
        # embeddings job uses), so there is nothing for the job writer to route.
        return []
