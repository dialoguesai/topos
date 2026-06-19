from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import MAX_JOB_MESSAGES
from .url_classification_core import classify_canonical_url_records
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
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "UrlClassificationSignalJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )
        classified = await classify_canonical_url_records(
            canonical_messages,
            engine=self._engine,
            progress_callback=progress_callback,
        )
        return [
            {
                "record_id": row.get("record_id"),
                "source_id": row.get("source_id"),
                "category": row.get("category"),
                "confidence": row.get("confidence"),
                "provider": row.get("provider", "huggingface"),
                "model": row.get("model"),
            }
            for row in classified
            if row.get("category")
        ]
