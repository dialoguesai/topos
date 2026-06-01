from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.jobs.topics")


class TopicsJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return "message_topics"

    async def enrich(
        self, 
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        logger.debug("%s: Topics enrichment stub: %d messages", self, len(canonical_messages))
        # Call progress callback to indicate completion (stub jobs complete instantly)
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        return []

    def get_job_name(self) -> str:
        return "topics"
