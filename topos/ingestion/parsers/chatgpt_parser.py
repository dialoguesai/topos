"""ChatGPT parser for ingestion layer."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from ..log_preview import field_preview
from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from ..validation.schema_registry import validate_schema
from .base import NormalizedRecord, Parser

logger = logging.getLogger("topos.ingestion.parser.chatgpt")


@dataclass
class ChatGPTParser(Parser):
    dataset_id: str
    _schema_id: str = "chatgpt.conversation.v1"  # Default to v1, can be overridden

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload
        role = payload.get("role", "").lower()
        sender_type = "human" if role == "user" else "assistant"
        created_at = payload.get("created_at")
        ts = ""
        if isinstance(created_at, (int, float)):
            from datetime import datetime, timezone

            ts = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
        elif isinstance(created_at, str):
            ts = created_at
        normalized = {
            "message_id": payload.get("id", raw.record_id),
            "dataset_id": self.dataset_id,
            "thread_id": payload.get("thread_id", ""),
            "ts": ts,
            "sender_type": sender_type,
            "content": payload.get("content", ""),
        }
        # Preserve _metadata if present (for conversation tree reconstruction)
        if "_metadata" in payload:
            normalized["_metadata"] = payload["_metadata"]
        logger.debug(
            "[PIPELINE:PARSER] Parsed record: message_id=%s, sender_type=%s, thread_id=%s, content_preview=%s",
            normalized["message_id"],
            normalized["sender_type"],
            normalized["thread_id"],
            field_preview(normalized.get("content")),
        )
        return NormalizedRecord(record_id=normalized["message_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        is_valid, error = validate_schema(record.payload, self.schema_id())
        errors = [] if is_valid else [error or "Invalid record"]
        logger.debug(
            "[PIPELINE:PARSER] Validation result: record_id=%s, is_valid=%s, errors=%s",
            record.record_id,
            is_valid,
            errors,
        )
        return ValidationResult(is_valid=is_valid, errors=errors, metadata={})

    def schema_id(self) -> str:
        return self._schema_id
