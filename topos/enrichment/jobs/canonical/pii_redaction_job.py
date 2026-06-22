"""Ingest-time PII redaction into canonical disclosure columns."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....disclosure.canonical_writer import DISCLOSURE_MODEL_SETTING, upsert_disclosure_fields
from ....disclosure.field_registry import (
    CANONICAL_ID_COLUMN,
    canonical_table_for_message,
    disclosure_column,
    disclosure_hash_column,
    fields_for_table,
)

logger = logging.getLogger("topos.enrichment.jobs.pii_redaction")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PiiRedactionJob(BaseEnrichmentJob):
    def __init__(
        self,
        *,
        name: Optional[str] = None,
        source_group: Optional[str] = None,
    ) -> None:
        super().__init__(name=name)
        self._source_group = source_group

    @property
    def writes_canonical(self) -> bool:
        return True

    def get_derived_table(self) -> str:
        return ""

    def get_job_name(self) -> str:
        return "pii_redaction"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        from ....core.state import get_db_connection
        from ....sanitization.privacy_filter import (
            apply_text_transform_with_privacy_filter,
            privacy_filter_enabled,
        )

        if not privacy_filter_enabled():
            logger.warning("[PIPELINE:ENRICHMENT] pii_redaction skipped: privacy filter unavailable")
            return []

        conn = get_db_connection()
        if conn is None:
            logger.warning("[PIPELINE:ENRICHMENT] pii_redaction skipped: no database connection")
            return []

        updated = 0
        total = len(canonical_messages)
        for idx, msg in enumerate(canonical_messages):
            if idx % 8 == 0:
                await asyncio.sleep(0)
            table = canonical_table_for_message(msg, source_group=self._source_group)
            if not table:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            record_id = (
                msg.get("message_id")
                or msg.get("entry_id")
                or msg.get("record_id")
                or msg.get("event_id")
            )
            if not record_id:
                if progress_callback:
                    progress_callback(idx + 1, total)
                continue
            record_id = str(record_id)

            patches: Dict[str, Any] = {}
            for field in fields_for_table(table):
                raw = msg.get(field)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                existing_hash = msg.get(disclosure_hash_column(field))
                if existing_hash == _content_hash(raw):
                    redacted = msg.get(disclosure_column(field))
                    if isinstance(redacted, str) and redacted.strip():
                        msg[field] = redacted
                    continue
                try:
                    redacted = apply_text_transform_with_privacy_filter(raw, "pii_redaction", {})
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pii_redaction failed table=%s field=%s record=%s: %s",
                        table,
                        field,
                        record_id,
                        exc,
                    )
                    continue
                patches[disclosure_column(field)] = redacted
                patches[disclosure_hash_column(field)] = _content_hash(raw)
                msg[field] = redacted

            if patches and upsert_disclosure_fields(
                conn,
                table,
                record_id,
                patches,
                model_id=DISCLOSURE_MODEL_SETTING,
            ):
                updated += 1
            if progress_callback:
                progress_callback(idx + 1, total)

        if updated:
            conn.commit()
        return [{"records_updated": updated}] if updated else []
