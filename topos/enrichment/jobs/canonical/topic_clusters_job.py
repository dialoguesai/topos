from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection
from ....features.signal.topic_clustering import (
    _resolved_topic_cluster_source_ids,
    recompute_topic_clusters,
    write_top_topics_signal_facts,
)
from ....storage.adapters.factory import AdapterFactory

logger = logging.getLogger("topos.enrichment.jobs.topic_clusters")


class TopicClusterJob(BaseEnrichmentJob):
    """Batch cluster canonical embeddings into memory_topic_map / top_topics."""

    def get_derived_table(self) -> str:
        return "topic_clusters"

    def get_job_name(self) -> str:
        return "topic_clusters"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages)

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]

        source_ids = {str(m.get("source_id") or "") for m in canonical_messages if m.get("source_id")}
        # Always cluster across all MVP query sources so a single-source ingest still participates.
        # does not wipe cross-source memory_topic_map rollups.
        scope_ids = list(_resolved_topic_cluster_source_ids())
        if source_ids:
            logger.info(
                "[PIPELINE:TOPIC_CLUSTERS] batch sources=%s; clustering scope=%s",
                sorted(source_ids),
                scope_ids,
            )
        sync_batch_id = None
        if canonical_messages:
            sync_batch_id = str(canonical_messages[0].get("sync_batch_id") or "")

        result = recompute_topic_clusters(
            conn,
            source_ids=scope_ids,
            sync_batch_id=sync_batch_id or None,
            min_records=3,
        )
        if result.get("status") == "skipped":
            logger.info("[PIPELINE:TOPIC_CLUSTERS] skipped: %s", result.get("reason"))
            if progress_callback:
                progress_callback(1, 1)
            return []

        try:
            bundle = AdapterFactory.from_runtime()
            write_top_topics_signal_facts(bundle, conn)
        except Exception as exc:
            logger.warning("[PIPELINE:TOPIC_CLUSTERS] top_topics write failed: %s", exc)

        if progress_callback:
            progress_callback(1, 1)

        return [
            {
                "cluster_id": label,
                "source_id": scope_ids[0] if scope_ids else "cross_source",
                "member_count": result.get("members_written", 0),
                "clusters_written": result.get("clusters_written", 0),
                "provider": "topos",
                "model": "kmeans_cosine_v1",
            }
            for label in (result.get("cluster_labels") or [])
        ]
