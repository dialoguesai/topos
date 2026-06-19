from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import (
    DIMENSION_SUMMARY_MAX_RECORDS,
    DIMENSION_SUMMARY_MAX_TOTAL_CHARS,
    DIMENSION_SUMMARY_RECORD_CHARS,
    MAX_JOB_MESSAGES,
)
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.dimension_summary")


def _truncate_records_for_summary(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    total_chars = 0
    for record in records[:DIMENSION_SUMMARY_MAX_RECORDS]:
        content = str(record.get("content") or record)[:DIMENSION_SUMMARY_RECORD_CHARS]
        if total_chars + len(content) > DIMENSION_SUMMARY_MAX_TOTAL_CHARS:
            remaining = DIMENSION_SUMMARY_MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                break
            content = content[:remaining]
        total_chars += len(content)
        row = dict(record) if isinstance(record, dict) else {}
        row["content"] = content
        out.append(row)
    return out


class DimensionSummaryJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "signal_summaries"

    def get_job_name(self) -> str:
        return "dimension_summary"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        if not canonical_messages:
            if progress_callback:
                progress_callback(0, 0)
            return []

        capped = canonical_messages[:MAX_JOB_MESSAGES]
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "DimensionSummaryJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )

        source_id = capped[0].get("source_id")
        records = _truncate_records_for_summary(capped)
        dimensions = ("memory", "profile", "interests", "relationships", "time")
        results: List[Dict[str, Any]] = []
        for idx, dimension in enumerate(dimensions):
            result = await run_engine_task(
                self._engine,
                task_id=f"dim_summary_{dimension}",
                subtype="raw_to_summary",
                source_id=source_id,
                record_ids=[],
                input_payload={"dimension": dimension, "records": records},
                provider="ollama",
                model="llama3.2:3b",
            )
            if result.status == "deferred":
                return [{"_deferred": True, "error": "ollama_unreachable"}]
            if result.status == "completed" and result.output.get("summary_text"):
                results.append(
                    {
                        "dimension": dimension,
                        "source_id": source_id,
                        "summary_text": result.output.get("summary_text"),
                        "provider": "ollama",
                        "model": result.output.get("model"),
                    }
                )
            if progress_callback:
                progress_callback(idx + 1, len(dimensions))
        return results
