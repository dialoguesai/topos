from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES, URL_CLASSIFICATION_BATCH_SIZE
from ._engine_runner import run_engine_task
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.url_classification_signal")


class UrlClassificationSignalJob(BaseEnrichmentJob):
    """Promote browser/activity URL classification into signal tags (Interests)."""

    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "signal_tags"

    def get_job_name(self) -> str:
        return "url_classification"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        messages = canonical_messages[:MAX_JOB_MESSAGES]
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "UrlClassificationSignalJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )

        results: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        total = len(messages)
        processed = 0

        for idx, msg in enumerate(messages):
            url = msg.get("url") or ""
            title = msg.get("title") or ""
            record_id = msg.get("event_id") or msg.get("record_id") or msg.get("id")
            source_id = msg.get("source_id")
            category = msg.get("category")
            confidence = msg.get("confidence")
            if category:
                results.append(
                    {
                        "record_id": record_id,
                        "source_id": source_id,
                        "category": category,
                        "confidence": confidence,
                        "provider": msg.get("provider", "huggingface"),
                        "model": msg.get("model"),
                    }
                )
            elif url:
                pending.append(
                    {
                        "record_id": record_id,
                        "source_id": source_id,
                        "url": url,
                        "title": title,
                    }
                )
            processed = idx + 1

        for start in range(0, len(pending), URL_CLASSIFICATION_BATCH_SIZE):
            batch = pending[start : start + URL_CLASSIFICATION_BATCH_SIZE]
            if not batch:
                continue
            result = await run_engine_task(
                self._engine,
                task_id=f"url_cls_batch_{start}",
                subtype="url_classification_batch",
                source_id=batch[0].get("source_id"),
                record_ids=[str(item["record_id"]) for item in batch if item.get("record_id")],
                input_payload={"items": batch},
            )
            if result.status == "completed":
                for item, row in zip(batch, result.output.get("items") or []):
                    results.append(
                        {
                            "record_id": item.get("record_id"),
                            "source_id": item.get("source_id"),
                            "category": row.get("category"),
                            "confidence": row.get("confidence"),
                            "provider": "huggingface",
                            "model": row.get("model") or result.output.get("model"),
                        }
                    )

        if progress_callback:
            progress_callback(total, total)
        return results
