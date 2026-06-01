"""Minimal parsers for messenger ingestion (iMessage, Signal).

Maps raw dict/row to normalized chat shape (message_id, thread_id, sender_type, content, ts).
Full implementation (reading from chat.db / Signal DB) is in Sprints 03 and 04.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from ..validation.schema_registry import validate_schema
from .base import NormalizedRecord, Parser

logger = logging.getLogger("topos.ingestion.parser.messenger")


def _normalize_messenger_payload(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    """Convert raw messenger record to normalized shape for conversation_messages."""
    role = (payload.get("role") or payload.get("sender_type") or "user").lower()
    sender_type = "human"  # Preserve legacy semantics; identity is carried in sender_id.
    created_at = payload.get("created_at") or payload.get("ts")
    ts = ""
    if isinstance(created_at, (int, float)):
        from datetime import datetime, timezone
        try:
            ts = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            # Keep ingestion resilient if a source record has an out-of-range timestamp.
            ts = ""
    elif isinstance(created_at, str):
        ts = created_at
    normalized = {
        "message_id": str(payload.get("id") or payload.get("message_id") or record_id),
        "dataset_id": dataset_id,
        "thread_id": str(payload.get("thread_id") or payload.get("conversation_id") or ""),
        "conversation_id": str(payload.get("thread_id") or payload.get("conversation_id") or ""),
        "ts": ts,
        "sender_type": sender_type,
        "content": (payload.get("content") or "") or "",
    }
    if "_metadata" in payload:
        normalized["_metadata"] = payload["_metadata"]
    if payload.get("sender_id") is not None:
        normalized["sender_id"] = str(payload["sender_id"])
    if payload.get("reply_to_message_id") is not None:
        normalized["reply_to_message_id"] = str(payload["reply_to_message_id"])
    if payload.get("message_type") is not None:
        normalized["message_type"] = str(payload["message_type"])
    if payload.get("event_type") is not None:
        normalized["event_type"] = str(payload["event_type"])
    return normalized


@dataclass
class ImessageParser(Parser):
    """Parser for iMessage normalized records (imessage.messages.v1)."""

    dataset_id: str
    _schema_id: str = "imessage.messages.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload
        normalized = _normalize_messenger_payload(payload, raw.record_id, self.dataset_id)
        return NormalizedRecord(record_id=normalized["message_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        is_valid, error = validate_schema(record.payload, self._schema_id)
        errors = [] if is_valid else [error or "Invalid record"]
        return ValidationResult(is_valid=is_valid, errors=errors, metadata={})

    def schema_id(self) -> str:
        return self._schema_id


@dataclass
class SignalParser(Parser):
    """Parser for Signal normalized records (signal.messages.v1)."""

    dataset_id: str
    _schema_id: str = "signal.messages.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload
        normalized = _normalize_messenger_payload(payload, raw.record_id, self.dataset_id)
        return NormalizedRecord(record_id=normalized["message_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        is_valid, error = validate_schema(record.payload, self._schema_id)
        errors = [] if is_valid else [error or "Invalid record"]
        return ValidationResult(is_valid=is_valid, errors=errors, metadata={})

    def schema_id(self) -> str:
        return self._schema_id
