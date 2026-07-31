from __future__ import annotations

import logging
import os
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

#: pipeline_jobs.kind for a deferred full topic-cluster consolidation.
TOPIC_CONSOLIDATION_KIND = "topic_consolidation"


def _defer_consolidation(conn) -> bool:
    """Queue the full recompute instead of running it inside this batch.

    Returns True when it was queued (caller should return early). Falls back to
    False — run inline, as before — if the queue is unavailable, because a
    consolidation that never happens is worse than a slow one.

    Coalescing is free: ``enqueue_job`` is idempotent on the key, so a hundred
    batches in a row leave exactly one pending consolidation.
    """
    if conn is None:
        return False
    if str(os.environ.get("TOPOS_DEFER_TOPIC_CONSOLIDATION", "on")).strip().lower() in (
        "0",
        "false",
        "off",
        "no",
    ):
        return False
    try:
        from ....pipeline.job_store import enqueue_job

        enqueue_job(
            conn,
            kind=TOPIC_CONSOLIDATION_KIND,
            payload={"reason": "consolidation_due"},
            idempotency_key="topic_consolidation:pending",
        )
        logger.info(
            "[PIPELINE:TOPIC_CLUSTERS] consolidation due; queued for after this batch"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PIPELINE:TOPIC_CLUSTERS] could not queue consolidation (%s); running inline",
            exc,
        )
        return False


_SMALL_BATCH_THRESHOLD = 20
_RECLUSTER_COOLDOWN_MINUTES = 30
_INCREMENTAL_MAX_BATCH = 200


def incremental_clusters_enabled() -> bool:
    return os.environ.get("TOPOS_INCREMENTAL_CLUSTERS", "on").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def _recent_recluster_exists(conn, *, cooldown_minutes: int = _RECLUSTER_COOLDOWN_MINUTES) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM topic_clusters WHERE updated_at >= datetime('now', ?) LIMIT 1",
            (f"-{int(cooldown_minutes)} minutes",),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _load_batch_embeddings(conn, record_ids: List[str]) -> List[Dict[str, Any]]:
    """Load the freshly written embeddings for this batch's records."""
    from ....features.signal.vector_codec import decode_vector

    if not record_ids:
        return []
    out: List[Dict[str, Any]] = []
    for start in range(0, len(record_ids), 200):
        chunk = record_ids[start : start + 200]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT embedding_id, record_id, source_id, text_preview,
                   vector_blob, vector_format, record_type
            FROM signal_embeddings
            WHERE record_id IN ({placeholders}) AND vector_blob IS NOT NULL
              AND chunk_index = 0
            """,
            chunk,
        ).fetchall()
        for embedding_id, record_id, source_id, preview, blob, fmt, record_type in rows:
            try:
                vector = decode_vector(blob, fmt or "json")
            except Exception:
                continue
            out.append(
                {
                    "embedding_id": embedding_id,
                    "record_id": record_id,
                    "source_id": source_id,
                    "text_preview": preview,
                    "vector": vector,
                    "record_type": record_type,
                }
            )
    return out


class TopicClusterJob(BaseEnrichmentJob):
    """Assign-first incremental clustering with debounced full consolidation."""

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

        from ....features.signal.incremental_clustering import (
            assign_embeddings,
            consolidation_due,
            load_cluster_centroids,
        )

        have_clusters = bool(load_cluster_centroids(conn))
        use_incremental = (
            incremental_clusters_enabled()
            and have_clusters
            and len(canonical_messages) <= _INCREMENTAL_MAX_BATCH
        )

        if use_incremental:
            record_ids = [
                str(m.get("event_id") or m.get("record_id") or m.get("message_id") or m.get("id") or "")
                for m in canonical_messages
            ]
            batch_embeddings = _load_batch_embeddings(conn, [r for r in record_ids if r])
            result = assign_embeddings(conn, batch_embeddings)
            logger.debug(
                "[PIPELINE:TOPIC_CLUSTERS] incremental assigned=%d pooled=%d",
                result["assigned"],
                result["pooled"],
            )
            if not consolidation_due(conn):
                self._refresh_top_topics(conn)
                if progress_callback:
                    progress_callback(1, 1)
                return []
            # Consolidation is a FULL recompute across every clustered source
            # (27 of them; 44s observed on 2026-07-30) and it used to run inline,
            # inside the ingest batch, holding the write gate. The incremental
            # assignment above has already placed this batch's records — the
            # recompute only moves centroids, which nothing downstream needs
            # before this batch finishes. So queue it durably and return.
            if _defer_consolidation(conn):
                self._refresh_top_topics(conn)
                if progress_callback:
                    progress_callback(1, 1)
                return []
            logger.debug("[PIPELINE:TOPIC_CLUSTERS] consolidation due; running full recompute")
        elif (
            have_clusters
            and len(canonical_messages) < _SMALL_BATCH_THRESHOLD
            and _recent_recluster_exists(conn)
        ):
            # Incremental path disabled: keep the small-batch cooldown gate.
            logger.debug(
                "[PIPELINE:TOPIC_CLUSTERS] skipped small batch (%d records) within cooldown",
                len(canonical_messages),
            )
            if progress_callback:
                progress_callback(1, 1)
            return []

        source_ids = {str(m.get("source_id") or "") for m in canonical_messages if m.get("source_id")}
        # Always cluster across all MVP query sources so a single-source ingest still participates.
        scope_ids = list(_resolved_topic_cluster_source_ids())
        if source_ids:
            logger.debug(
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
            logger.debug("[PIPELINE:TOPIC_CLUSTERS] skipped: %s", result.get("reason"))
            if progress_callback:
                progress_callback(1, 1)
            return []

        self._refresh_top_topics(conn)

        if progress_callback:
            progress_callback(1, 1)

        return [
            {
                "cluster_id": label,
                "source_id": scope_ids[0] if scope_ids else "cross_source",
                "member_count": result.get("members_written", 0),
                "clusters_written": result.get("clusters_written", 0),
                "ids_preserved": result.get("ids_preserved", 0),
                "provider": "topos",
                "model": "kmeans_cosine_v1",
            }
            for label in (result.get("cluster_labels") or [])
        ]

    def _refresh_top_topics(self, conn) -> None:
        try:
            bundle = AdapterFactory.from_runtime()
            write_top_topics_signal_facts(bundle, conn)
        except Exception as exc:
            logger.warning("[PIPELINE:TOPIC_CLUSTERS] top_topics write failed: %s", exc)
