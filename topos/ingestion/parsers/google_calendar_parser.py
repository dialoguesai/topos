"""Google Calendar event parser for ingestion layer (remote connectors).

Near-identity passthrough: GoogleCalendarMapper reads the raw Google Calendar
event fields directly (status, transparency, start/end, attendees,
organizer, …), so this parser's only job is to stamp record_id/dataset_id
and preserve every field untouched — the same shape as
``github_activity_parser.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser

logger = logging.getLogger("topos.ingestion.parser.google_calendar")


@dataclass
class GoogleCalendarParser(Parser):
    """Parser for gcal_events records shaped like Google Calendar API `events` resources."""

    dataset_id: str
    _schema_id: str = "gcal.events.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = dict(raw.payload)
        record_id = str(payload.get("id") or raw.record_id)
        payload.setdefault("record_id", record_id)
        payload["dataset_id"] = self.dataset_id
        logger.debug(
            "[PIPELINE:PARSER] Parsed gcal event: id=%s, status=%s",
            record_id[:32],
            payload.get("status"),
        )
        return NormalizedRecord(record_id=record_id, payload=payload)

    def validate(self, record: RawRecord) -> ValidationResult:
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
        if not str(payload.get("id") or "").strip():
            return ValidationResult(
                is_valid=False, errors=["Missing required field: id"], metadata={}
            )
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id
