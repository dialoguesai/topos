from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES, TOPIC_BATCH_CONCURRENCY
from ....config.signal_extraction import get_signal_extraction_model_request
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.goal_extraction")

GOAL_BATCH_CONCURRENCY = max(
    1,
    int(os.environ.get("TOPOS_GOAL_BATCH_CONCURRENCY", "2")),
)


class GoalExtractionJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "user_goals"

    def get_job_name(self) -> str:
        return "goal_extraction"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        messages = canonical_messages[:MAX_JOB_MESSAGES]
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "GoalExtractionJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )

        work: List[Dict[str, Any]] = []
        for msg in messages:
            message_id = msg.get("message_id") or msg.get("id")
            content = msg.get("content", "")
            if message_id and content:
                work.append(
                    {
                        "message_id": message_id,
                        "content": str(content),
                        "source_id": msg.get("source_id"),
                    }
                )

        total = len(work)
        results: List[Dict[str, Any]] = []
        deferrals = 0
        sem = asyncio.Semaphore(GOAL_BATCH_CONCURRENCY)
        processed = 0

        async def _one(item: Dict[str, Any]) -> List[Dict[str, Any]]:
            nonlocal deferrals
            async with sem:
                engine_provider, extraction_model = get_signal_extraction_model_request()
                result = await run_engine_task(
                    self._engine,
                    task_id=f"goals_{item['message_id']}",
                    subtype="goal_extraction",
                    source_id=item.get("source_id"),
                    record_ids=[str(item["message_id"])],
                    input_payload={"text": item["content"]},
                    provider=engine_provider,
                    model=extraction_model,
                )
                if result.status == "deferred":
                    deferrals += 1
                    return []
                if result.status != "completed":
                    return []
                rows: List[Dict[str, Any]] = []
                for goal in result.output.get("goals") or []:
                    goal_text = goal.get("text")
                    if not goal_text:
                        continue
                    rows.append(
                        {
                            "message_id": item["message_id"],
                            "source_id": item.get("source_id"),
                            "goal_text": goal_text,
                            "confidence": goal.get("confidence"),
                            "horizon": goal.get("horizon"),
                            "provider": "ollama",
                            "model": result.output.get("model"),
                        }
                    )
                return rows

        for start in range(0, total, GOAL_BATCH_CONCURRENCY):
            chunk = work[start : start + GOAL_BATCH_CONCURRENCY]
            batch_out = await asyncio.gather(*[_one(item) for item in chunk])
            for rows in batch_out:
                if rows:
                    results.extend(rows)
            processed += len(chunk)
            if progress_callback:
                progress_callback(min(processed, total), total)

        if deferrals == total and total > 0 and not results:
            return [{"_deferred": True, "error": "ollama_unreachable"}]
        if deferrals:
            logger.warning(
                "GoalExtractionJob skipped %d/%d messages (Ollama deferred)",
                deferrals,
                total,
            )
        return results
