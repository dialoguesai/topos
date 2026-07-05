"""StatisticsJob: fold canonical batches into incremental stats; debounced promotion.

Fold is O(batch) and runs on every batch (the "add one blue ball" update — no
table rescans). Promotion of human-readable stat_insight facts into
signal_facts is debounced: it runs when enough rows have been folded since the
last promotion, or when the last promotion is stale.

Gated by TOPOS_STATS (default on).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection

logger = logging.getLogger("topos.enrichment.jobs.statistics")

_PROMOTE_EVERY_ROWS = 20
_PROMOTE_STALE_MINUTES = 15
_META_STAT_ID = "_promote_meta"


def stats_enabled() -> bool:
    return os.environ.get("TOPOS_STATS", "on").strip().lower() not in ("0", "false", "off", "no")


class StatisticsJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""  # writes stat_state directly; promotion writes signal_facts

    def get_job_name(self) -> str:
        return "statistics"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages) and stats_enabled()

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]

        from ....features.stats.engine import StatsEngine

        engine = StatsEngine(conn)
        result = engine.fold_batch(canonical_messages)
        promoted = 0
        if self._should_promote(conn, result["rows_folded"]):
            try:
                from ....storage.adapters.factory import AdapterFactory

                bundle = AdapterFactory.create("local_database", conn=conn)
                promoted = engine.promote_insights(bundle)
                self._record_promotion(conn)
            except Exception as exc:
                logger.warning("stat insight promotion failed: %s", exc)

        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug(
            "[PIPELINE:STATISTICS] folded=%d skipped_seen=%d promoted=%d",
            result["rows_folded"],
            result["rows_skipped_seen"],
            promoted,
        )
        # No derived-table records; orchestrator accounting only.
        return []

    def _should_promote(self, conn, rows_folded: int) -> bool:
        if rows_folded <= 0:
            return False
        row = conn.execute(
            "SELECT state_json, updated_at FROM stat_state WHERE stat_id=? AND group_key='' AND bucket_date=''",
            (_META_STAT_ID,),
        ).fetchone()
        if row is None:
            return True
        import json

        try:
            pending = int(json.loads(row[0]).get("pending") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pending = 0
        pending += rows_folded
        conn.execute(
            "UPDATE stat_state SET state_json=? WHERE stat_id=? AND group_key='' AND bucket_date=''",
            (json.dumps({"pending": pending}), _META_STAT_ID),
        )
        if pending >= _PROMOTE_EVERY_ROWS:
            return True
        stale = conn.execute(
            "SELECT 1 FROM stat_state WHERE stat_id=? AND updated_at < datetime('now', ?)",
            (_META_STAT_ID, f"-{_PROMOTE_STALE_MINUTES} minutes"),
        ).fetchone()
        return stale is not None

    def _record_promotion(self, conn) -> None:
        import json

        conn.execute(
            """
            INSERT INTO stat_state (stat_id, group_key, bucket_date, state_json, updated_at)
            VALUES (?, '', '', ?, datetime('now'))
            ON CONFLICT(stat_id, group_key, bucket_date) DO UPDATE SET
                state_json=excluded.state_json, updated_at=datetime('now')
            """,
            (_META_STAT_ID, json.dumps({"pending": 0})),
        )
        conn.commit()
