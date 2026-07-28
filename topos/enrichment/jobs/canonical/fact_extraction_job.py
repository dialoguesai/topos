"""FactExtractionJob: assert atomic owner facts from canonical batches.

Rules-only floor plus optional LLM pass (see features/facts/extract.py);
gated by TOPOS_FACTS (default on).

Sync extraction (incl. fact_llm / Ollama) is offloaded via asyncio.to_thread so
the engine event loop can keep servicing control-plane keepalive and UI proxy
RPCs while enrichment runs in the background.
"""

from __future__ import annotations

import asyncio
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

        # Offload the full sync path (rules + fact_llm). extract_facts_from_batch
        # may block for tens of seconds on Ollama; running it on the event-loop
        # thread starves CP ping/pong and UI signal RPCs.
        try:
            written = await asyncio.to_thread(
                extract_facts_from_batch, conn, canonical_messages
            )
        except asyncio.CancelledError:
            from ....runtime_shutdown import request_shutdown

            # Cancelling the await does not kill the worker thread; the shutdown
            # flag lets fact_llm stop scheduling further Ollama calls.
            request_shutdown("fact_extraction_cancelled")
            raise
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug("[PIPELINE:FACTS] asserted %d facts", written)
        # FactStore already persisted the facts above; the _written sentinel
        # carries the count so orchestrator records_created reflects the real
        # writes instead of a hardcoded 0 (nothing here goes through
        # write_signal_records).
        return [{"_written": int(written or 0)}]
