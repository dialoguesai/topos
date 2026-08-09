"""Attention triage enrichment job (PLAN_ATTENTION_TRIAGE.md M2).

Per canonical batch: re-triage every day touched by the batch. Idempotent —
verdicts are (day, record_id) upserts and the signal objects no-op when
unchanged, so re-running a day is safe and cheap.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ....core.state import get_db_connection
from ....features.triage.daily import run_daily_triage
from ..base import BaseEnrichmentJob

logger = logging.getLogger(__name__)

_EVENT_AT_FIELDS = ("event_at", "occurred_at", "entry_at", "ts", "created_at")


class AttentionTriageJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""

    def get_job_name(self) -> str:
        return "attention_triage"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages)

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        if get_db_connection() is None:
            return [{"_deferred": True, "error": "database_unavailable"}]
        days = set()
        for row in canonical_messages:
            for f in _EVENT_AT_FIELDS:
                v = row.get(f)
                if v:
                    days.add(str(v)[:10])
                    break
        done = 0
        for day_iso in sorted(days):
            # A day's re-triage holds the write gate — a blocking OS lock — for
            # its verdict upserts and signal objects. Held on the event loop it
            # stalls every coroutine, including the control-plane keepalive, so
            # each day runs on a worker thread against that thread's own
            # connection. One day per hop also keeps progress incremental.
            def _triage(_day: str = day_iso) -> Optional[Dict[str, Any]]:
                conn = get_db_connection()
                if conn is None:
                    return None
                return run_daily_triage(conn, _day)

            try:
                summary = await asyncio.to_thread(_triage)
                if summary is not None:
                    logger.info("attention_triage %s: %s", day_iso, summary.get("quadrants"))
            except Exception:  # one bad day must not sink the batch
                logger.exception("attention_triage failed for %s", day_iso)
            done += 1
            if progress_callback:
                progress_callback(done, len(days))

        def _award() -> Optional[List[Any]]:
            conn = get_db_connection()
            if conn is None:
                return None
            from ....features.triage.badges import award_badges

            return award_badges(conn)

        try:
            fresh = await asyncio.to_thread(_award)
            if fresh:
                logger.info("badges awarded: %s", fresh)
        except Exception:
            logger.exception("badge award pass failed")
        if progress_callback:
            progress_callback(len(days), len(days))
        return []
