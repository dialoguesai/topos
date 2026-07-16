from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.jobs.availability_scores")


def _row_is_busy(row: Dict[str, Any]) -> bool:
    """Busy/free per row, reading the real is_busy/status columns.

    Cancelled events never block a slot regardless of is_busy. Rows with no
    is_busy value at all (pre-migration rows, or a calendar lane that never
    set it) default to busy — the same conservative fallback the old
    row-count formula implied by treating every calendar row as busy.
    """
    if str(row.get("status") or "").strip().lower() == "cancelled":
        return False
    is_busy = row.get("is_busy")
    if is_busy is None:
        return True
    if isinstance(is_busy, str):
        return is_busy.strip().lower() in ("1", "true", "yes")
    return bool(is_busy)


class AvailabilityScoresJob(BaseEnrichmentJob):
    """Calendar-derived availability scores; no output when no calendar rows.

    Writing a placeholder row here would count as measured signal in data
    health, so a batch without calendar data must produce nothing.

    v1 real busy-fraction: the score is 1 minus the fraction of calendar rows
    that are actually busy (is_busy/status), not a row-count identity that
    was always exactly 0.5. This is still row-count-weighted, not
    duration-weighted over a real time window — full interval materialization
    (merging overlapping busy_blocks into true availability_windows) is a
    noted follow-up, not built here (PLAN_CANONICAL_CALENDAR_DOCUMENTS §B5).
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
            busy = sum(1 for row in calendar_rows if _row_is_busy(row))
            score = max(0.0, min(1.0, 1.0 - (busy / len(calendar_rows))))
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
