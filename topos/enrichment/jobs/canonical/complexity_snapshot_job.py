"""Complexity snapshot enrichment job (PLAN_COMPLEXITY_DATA_PAGE.md M3).

Keeps the /data/complexity dashboard warm: after canonical batches land,
recompute the complexity summary snapshot (focus/structure/breadth/confidence
readings + influence threads) so reads serve the cache instead of paying the
~1s cold compute. Throttled — the full-window readings barely move within a
few hours, so a batch only recomputes when the cached summary is stale.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ....core.state import get_db_connection
from ..base import BaseEnrichmentJob

logger = logging.getLogger(__name__)

# Recompute at most this often per node; on-demand recompute=true stays live.
STALENESS = timedelta(hours=6)


def _summary_updated_at(conn: sqlite3.Connection) -> Optional[datetime]:
    try:
        row = conn.execute(
            "SELECT MAX(updated_at) AS updated_at FROM complexity_snapshots WHERE metric_set='summary'"
        ).fetchone()
    except sqlite3.OperationalError:  # pre-migration DB — treat as stale
        return None
    raw = row["updated_at"] if row is not None else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ComplexitySnapshotJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""

    def get_job_name(self) -> str:
        return "complexity_snapshot"

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

        updated_at = _summary_updated_at(conn)
        if updated_at is not None and datetime.now(timezone.utc) - updated_at < STALENESS:
            logger.debug("complexity_snapshot fresh (updated %s); skipping", updated_at)
            if progress_callback:
                progress_callback(1, 1)
            return []

        try:
            from ....features.complexity.engine import get_complexity_summary

            summary = await asyncio.to_thread(get_complexity_summary, conn, recompute=True)
            logger.info(
                "complexity_snapshot recomputed: focus=%.1f threads=%d",
                float(summary.get("focus_index", {}).get("score") or 0.0),
                len(summary.get("influence_threads") or []),
            )
        except Exception:  # a failed analytic must not sink the batch
            logger.exception("complexity_snapshot recompute failed")
        if progress_callback:
            progress_callback(1, 1)
        return []
