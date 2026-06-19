from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.sentiment")


class SentimentJob(BaseEnrichmentJob):
    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "message_sentiment"

    def get_job_name(self) -> str:
        return "sentiment"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        total = len(canonical_messages)
        for idx, msg in enumerate(canonical_messages):
            if idx % 10 == 0:
                await asyncio.sleep(0)
            message_id = msg.get("message_id") or msg.get("id")
            content = msg.get("content", "")
            source_id = msg.get("source_id")
            if not message_id or not content:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            result = await run_engine_task(
                self._engine,
                task_id=f"sentiment_{message_id}",
                subtype="sentiment_classification",
                source_id=source_id,
                record_ids=[str(message_id)],
                input_payload={"text": content},
            )
            if result.status != "completed":
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            results.append(
                {
                    "message_id": message_id,
                    "source_id": source_id,
                    "label": result.output.get("label"),
                    "score": result.output.get("score"),
                    "provider": result.output.get("provider", "huggingface"),
                    "model": result.output.get("model"),
                }
            )
            if progress_callback:
                progress_callback(idx + 1, total)
        return results
