"""Emotion classification enrichment via the Engine (HF or Ollama adapter)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ...progress_bar import ProgressBar
from ....engine import Engine
from ....engine.tasks import ModelRequest, ProcessingTask

logger = logging.getLogger("topos.enrichment.jobs.emo_27")


class Emo27Job(BaseEnrichmentJob):
    """Emotion classification enrichment using the Engine (HF or Ollama)."""

    def __init__(self, *, name: Optional[str] = None):
        super().__init__(name=name)
        self._engine = Engine()

    def get_derived_table(self) -> str:
        return "message_emotions"

    def get_job_name(self) -> str:
        return "emo_27"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Enrich messages with emotion classifications via Engine.run(task)."""
        logger.debug("[PIPELINE:ENRICHMENT] %s: processing %d messages", self, len(canonical_messages))
        results = []
        total_messages = len(canonical_messages)

        with ProgressBar(total=total_messages, desc=str(self)) as pbar:
            for msg_idx, msg in enumerate(canonical_messages):
                if msg_idx % 10 == 0:
                    await asyncio.sleep(0)
                message_id = msg.get("message_id") or msg.get("id")
                content = msg.get("content", "")
                source_id = msg.get("source_id")

                if not message_id or not content:
                    pbar.update(1)
                    if progress_callback:
                        progress_callback(msg_idx + 1, total_messages)
                    continue

                try:
                    task = ProcessingTask(
                        id=f"emo27_{message_id}",
                        type="enrichment",
                        subtype="emotion_classification",
                        source_id=source_id,
                        record_ids=[message_id],
                        input={"text": content},
                        model_request=ModelRequest(provider="huggingface"),
                    )
                    result = await asyncio.to_thread(self._engine.run, task)
                    if result.status != "completed":
                        logger.warning(
                            "[PIPELINE:ENRICHMENT] %s: Engine failed for message %s: %s",
                            self, message_id, result.error or result.status,
                        )
                        pbar.update(1)
                        if progress_callback:
                            progress_callback(msg_idx + 1, total_messages)
                        continue
                    out = result.output
                    results.append({
                        "message_id": message_id,
                        "source_id": source_id,
                        "emotion_label": out.get("emotion_label"),
                        "confidence": out.get("confidence"),
                        "all_emotions": out.get("all_emotions", []),
                        "model": out.get("model", ""),
                    })
                except Exception as e:
                    logger.error(
                        "[PIPELINE:ENRICHMENT] %s: Failed to enrich message %s: %s",
                        self, message_id, e,
                    )
                    pbar.update(1)
                    if progress_callback:
                        progress_callback(msg_idx + 1, total_messages)
                    continue
                pbar.update(1)
                if progress_callback:
                    progress_callback(msg_idx + 1, total_messages)

        logger.debug("[PIPELINE:ENRICHMENT] %s: created %d results", self, len(results))
        return results
