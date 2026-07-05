"""FactExtractionJob: assert atomic owner facts from canonical batches.

Rules-only (see features/facts/extract.py); gated by TOPOS_FACTS (default on).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection

logger = logging.getLogger("topos.enrichment.jobs.facts")


def facts_enabled() -> bool:
    return os.environ.get("TOPOS_FACTS", "on").strip().lower() not in ("0", "false", "off", "no")


class FactExtractionJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""  # writes signal_objects directly

    def get_job_name(self) -> str:
        return "facts"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages) and facts_enabled()

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]
        from ....features.facts.extract import extract_facts_from_batch

        written = extract_facts_from_batch(conn, canonical_messages)
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug("[PIPELINE:FACTS] asserted %d facts", written)
        return []
