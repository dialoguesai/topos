from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES
from .url_classification_core import classify_canonical_url_records
from ....engine import Engine

logger = logging.getLogger("topos.enrichment.jobs.url_classification_enrichment")


class UrlClassificationEnrichmentJob(BaseEnrichmentJob):
    """Classify URLs from canonical activity_events into browser_url_classification."""

    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "browser_url_classification"

    def get_job_name(self) -> str:
        return "url_classification"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return any(
            isinstance(msg.get("url"), str) and msg.get("url", "").strip()
            for msg in canonical_messages
        )

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "UrlClassificationEnrichmentJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )
        classified = await classify_canonical_url_records(
            canonical_messages,
            engine=self._engine,
            progress_callback=progress_callback,
        )
        rows: List[Dict[str, Any]] = []
        for item in classified:
            record_id = item.get("record_id") or item.get("event_id")
            if not record_id:
                continue
            rows.append(
                {
                    "enriched_from_table": "activity_events",
                    "record_id": record_id,
                    "dataset_id": item.get("dataset_id"),
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "url_category": item.get("category"),
                    "url_confidence": item.get("confidence"),
                    "model_name": item.get("model"),
                    "source_id": item.get("source_id"),
                    # passthrough for pipeline merge + signal derive
                    "category": item.get("category"),
                    "confidence": item.get("confidence"),
                    "model": item.get("model"),
                    "event_id": record_id,
                }
            )
        return rows
