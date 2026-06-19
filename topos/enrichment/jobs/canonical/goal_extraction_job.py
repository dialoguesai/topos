from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.goal_extraction")


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
        results: List[Dict[str, Any]] = []
        total = len(canonical_messages)
        for idx, msg in enumerate(canonical_messages):
            message_id = msg.get("message_id") or msg.get("id")
            content = msg.get("content", "")
            source_id = msg.get("source_id")
            if not message_id or not content:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            result = await run_engine_task(
                self._engine,
                task_id=f"goals_{message_id}",
                subtype="goal_extraction",
                source_id=source_id,
                record_ids=[str(message_id)],
                input_payload={"text": content},
                provider="ollama",
                model="llama3.2:3b",
            )
            if result.status == "deferred":
                return [{"_deferred": True, "error": "ollama_unreachable"}]
            if result.status != "completed":
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            for goal in result.output.get("goals") or []:
                results.append(
                    {
                        "message_id": message_id,
                        "source_id": source_id,
                        "goal_text": goal.get("text"),
                        "confidence": goal.get("confidence"),
                        "horizon": goal.get("horizon"),
                        "provider": "ollama",
                        "model": result.output.get("model"),
                    }
                )
            if progress_callback:
                progress_callback(idx + 1, total)
        return results
