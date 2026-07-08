from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.jobs.availability_scores")


class AvailabilityScoresJob(BaseEnrichmentJob):
    """Calendar-derived availability scores; no output when no calendar rows.

    Writing a placeholder row here would count as measured signal in data
    health, so a batch without calendar data must produce nothing.
    """

    def get_derived_table(self) -> str:
        return "signal_scores"

    def get_job_name(self) -> str:
        return "availability_scores"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        calendar_rows = [
            m for m in canonical_messages if m.get("event_id") or m.get("activity_type") == "calendar"
        ]
        source_id = canonical_messages[0].get("source_id") if canonical_messages else None
        results: List[Dict[str, Any]] = []
        if calendar_rows:
            busy = len(calendar_rows)
            score = max(0.0, min(1.0, 1.0 - (busy / max(len(calendar_rows), 1)) * 0.5))
            results = [
                {
                    "source_id": source_id,
                    "label": "availability",
                    "score": score,
                    "provider": "rules",
                    "model": "availability_rules_v1",
                }
            ]
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        return results
