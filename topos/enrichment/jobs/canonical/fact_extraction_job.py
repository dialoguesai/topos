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
import threading
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

        # Cancellation is scoped to THIS batch. Cancelling the await does not
        # kill the worker thread, so it still needs a cooperative signal — but
        # that signal must not outlive the batch. This used to call
        # runtime_shutdown.request_shutdown(), a process-lifetime flag only an
        # app startup could clear: one cancelled batch left every later batch
        # returning 0 facts, with the job still reporting success, until the
        # node was restarted.
        cancel = threading.Event()
        llm_stats: Dict[str, Any] = {}
        try:
            written = await asyncio.to_thread(
                extract_facts_from_batch,
                conn,
                canonical_messages,
                cancel=cancel,
                llm_stats=llm_stats,
            )
        except asyncio.CancelledError:
            cancel.set()
            raise
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        logger.debug("[PIPELINE:FACTS] asserted %d facts", written)
        # FactStore already persisted the facts above; the _written sentinel
        # carries the count so orchestrator records_created reflects the real
        # writes instead of a hardcoded 0 (nothing here goes through
        # write_signal_records).
        result: Dict[str, Any] = {"_written": int(written or 0)}
        if llm_stats.get("stopped"):
            # A batch that stopped early is not a batch that found nothing —
            # say so in the job record rather than letting a quiet lane read as
            # a successful empty pass.
            result["_facts_llm_stopped"] = True
            result["_facts_llm_stop_reason"] = str(llm_stats.get("stop_reason") or "stopped")
            result["_facts_llm_unprocessed"] = int(llm_stats.get("unprocessed") or 0)
            logger.warning(
                "[PIPELINE:FACTS] LLM pass stopped early (%s); %d row(s) left for the next pass",
                result["_facts_llm_stop_reason"],
                result["_facts_llm_unprocessed"],
            )
        return [result]
