"""TimelineJob: maintain the unified temporal projection (insert-only per batch)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection
from ....features.timeline_projection import project_timeline_rows

logger = logging.getLogger("topos.enrichment.jobs.timeline")


class TimelineJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""

    def get_job_name(self) -> str:
        return "timeline"

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
        result = project_timeline_rows(conn, canonical_messages)
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug(
            "[PIPELINE:TIMELINE] candidates=%d written=%d existing=%d excluded=%d skipped_timestamp=%d",
            result.candidates,
            result.written,
            result.existing,
            result.excluded,
            result.missing_timestamp,
        )
        return []
