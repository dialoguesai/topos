"""Emotion classification enrichment via the Engine (HF or Ollama adapter)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES
from ....features.provenance.roles import record_role
from ._engine_runner import run_engine_task
from .brief_fallback import prepare_signal_record
from ...progress_bar import ProgressBar
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.emo_27")

_EMO_BATCH_SIZE = 32


class Emo27Job(BaseEnrichmentJob):
    """Emotion classification enrichment using the Engine (HF or Ollama)."""

    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

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
        messages = canonical_messages[:MAX_JOB_MESSAGES]
        logger.debug("[PIPELINE:ENRICHMENT] %s: processing %d messages", self, len(messages))
        work: List[Dict[str, Any]] = []
        for msg in messages:
            prepared = prepare_signal_record(msg)
            message_id = prepared.get("message_id") or prepared.get("id")
            content = prepared.get("content", "")
            source_id = prepared.get("source_id")
            if message_id and content:
                work.append(
                    {
                        "id": message_id,
                        "message_id": message_id,
                        "text": content,
                        "source_id": source_id,
                        # P1.3 (PLAN_PROVENANCE_SPLIT): message_emotions is
                        # per-message data, so EVERY row is still classified —
                        # but each emitted record carries whose words it
                        # classified, so owner-wellbeing aggregation can filter
                        # to authored(+addressed) downstream. Stripped from the
                        # engine payload in _classify_batch (payload contract
                        # unchanged); persisted via message_emotions
                        # payload_json (wiki schema stores the whole record).
                        "role": record_role(msg, table=str(msg.get("_table") or "")),
                    }
                )

        results: List[Dict[str, Any]] = []
        total = len(work)
        processed = 0
        with ProgressBar(total=total or 1, desc=str(self)) as pbar:
            for i in range(0, len(work), _EMO_BATCH_SIZE):
                batch = work[i : i + _EMO_BATCH_SIZE]
                batch_results = await self._classify_batch(batch)
                results.extend(batch_results)
                processed += len(batch)
                pbar.update(len(batch))
                if progress_callback:
                    progress_callback(processed, total)

        logger.debug("[PIPELINE:ENRICHMENT] %s: created %d results", self, len(results))
        return results

    async def _classify_batch(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not items:
            return []
        record_ids = [str(i["message_id"]) for i in items]
        source_id = items[0].get("source_id")
        # role is job-side metadata; the engine's classification payload keeps
        # its exact pre-P1.3 shape (id/message_id/text/source_id only).
        payload_items = [{k: v for k, v in item.items() if k != "role"} for item in items]
        try:
            result = await run_engine_task(
                self._engine,
                task_id=f"emo27_batch_{record_ids[0]}",
                subtype="emotion_classification_batch",
                source_id=source_id,
                record_ids=record_ids,
                input_payload={"items": payload_items},
            )
            if result.status != "completed":
                logger.warning(
                    "[PIPELINE:ENRICHMENT] %s: Engine batch failed: %s",
                    self,
                    result.error or result.status,
                )
                return []
            out_items = result.output.get("items") or []
            rows: List[Dict[str, Any]] = []
            for item, out in zip(items, out_items):
                rows.append(
                    {
                        "message_id": item["message_id"],
                        "source_id": item.get("source_id"),
                        "emotion_label": out.get("emotion_label"),
                        "confidence": out.get("confidence"),
                        "all_emotions": out.get("all_emotions", []),
                        "model": out.get("model", ""),
                        # Whose words this emotion belongs to (record_role).
                        "role": item.get("role"),
                    }
                )
            return rows
        except Exception as exc:
            logger.error("[PIPELINE:ENRICHMENT] %s: batch classify failed: %s", self, exc)
            return []
