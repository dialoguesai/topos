"""TimelineJob: maintain the unified temporal projection (insert-only per batch)."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection
from ....features.stats.engine import _record_id, _table_for_row

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
        from ....features.stats.definitions import row_event_ts

        written = 0
        for row in canonical_messages:
            ts = row_event_ts(row)
            record_id = _record_id(row)
            if ts is None or not record_id:
                continue
            table = _table_for_row(row)
            conn.execute(
                """
                INSERT OR REPLACE INTO timeline (
                    event_at, record_id, source_id, canonical_table,
                    record_type, entity_ids_json, signal_dimension
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts.isoformat(),
                    record_id,
                    row.get("source_id"),
                    table or None,
                    row.get("record_type"),
                    json.dumps(row.get("entity_ids") or []),
                    row.get("signal_dimension"),
                ),
            )
            written += 1
        conn.commit()
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug("[PIPELINE:TIMELINE] wrote %d rows", written)
        return []
