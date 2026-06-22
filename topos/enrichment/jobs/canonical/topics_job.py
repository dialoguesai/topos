from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES, TOPIC_BATCH_CONCURRENCY
from ....config.signal_extraction import get_signal_extraction_model_request, get_signal_extraction_provider
from ....config.settings import settings
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.topics")


class TopicsJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "message_topics"

    def get_job_name(self) -> str:
        return "topics"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        messages = canonical_messages[:MAX_JOB_MESSAGES]
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "TopicsJob capped input from %d to %d messages",
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
        deferred = False
        sem = asyncio.Semaphore(TOPIC_BATCH_CONCURRENCY)
        processed = 0

        async def _one(item: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
            nonlocal deferred
            async with sem:
                if deferred:
                    return None
                engine_provider, extraction_model = get_signal_extraction_model_request()
                configured_provider = get_signal_extraction_provider()
                result = await run_engine_task(
                    self._engine,
                    task_id=f"topics_{item['message_id']}",
                    subtype="topic_extraction",
                    source_id=item.get("source_id"),
                    record_ids=[str(item["message_id"])],
                    input_payload={"text": item["content"]},
                    provider=engine_provider,
                    model=extraction_model,
                )
                if result.status == "deferred":
                    deferred = True
                    return None
                if result.status != "completed":
                    return []
                rows: List[Dict[str, Any]] = []
                for topic in result.output.get("topics") or []:
                    rows.append(
                        {
                            "message_id": item["message_id"],
                            "source_id": item.get("source_id"),
                            "topic": topic.get("label"),
                            "confidence": topic.get("confidence"),
                            "provider": configured_provider,
                            "model": result.output.get("model"),
                        }
                    )
                return rows

        for start in range(0, total, TOPIC_BATCH_CONCURRENCY):
            if deferred:
                break
            chunk = work[start : start + TOPIC_BATCH_CONCURRENCY]
            batch_out = await asyncio.gather(*[_one(item) for item in chunk])
            for rows in batch_out:
                if rows:
                    results.extend(rows)
            processed += len(chunk)
            if progress_callback:
                progress_callback(min(processed, total), total)
            logger.info("TopicsJob progress %d/%d", min(processed, total), total)

        if deferred and not results:
            return [{"_deferred": True, "error": "ollama_unreachable"}]
        return results
