"""Parser for VoxTerm transcript segments (ui_stream / app_ingest)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser
from .messenger_parser import _normalize_messenger_payload

logger = logging.getLogger("topos.ingestion.parser.voxterm")


def _voxterm_to_messenger_shape(payload: Dict[str, Any], record_id: str) -> Dict[str, Any]:
    """Map flat VoxTerm segment fields to messenger parser input."""
    created_at = payload.get("event_at") or payload.get("created_at") or payload.get("ts")
    messenger = {
        "id": str(payload.get("message_id") or record_id),
        "message_id": str(payload.get("message_id") or record_id),
        "thread_id": str(payload.get("conversation_id") or ""),
        "conversation_id": str(payload.get("conversation_id") or ""),
        "role": "user",
        "sender_type": str(payload.get("sender_type") or "human"),
        "sender_id": payload.get("sender_id"),
        "content": str(payload.get("content") or ""),
        "created_at": created_at,
    }
    metadata: Dict[str, Any] = {}
    for key in ("origin_device", "batch_index", "segment_index", "location"):
        if payload.get(key) is not None:
            metadata[key] = payload[key]
    if metadata:
        messenger["_metadata"] = metadata
    return messenger


@dataclass
class VoxtermTranscriptParser(Parser):
    """Parser for voxterm.transcript.v1 ui_stream records."""

    dataset_id: str
    _schema_id: str = "voxterm.transcript.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload
        shaped = _voxterm_to_messenger_shape(payload, raw.record_id)
        normalized = _normalize_messenger_payload(shaped, shaped["message_id"], self.dataset_id)
        return NormalizedRecord(record_id=normalized["message_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
        errors = []
        if not str(payload.get("message_id") or payload.get("id") or record.record_id or "").strip():
            errors.append("Missing required field: message_id")
        if not str(payload.get("conversation_id") or "").strip():
            errors.append("Missing required field: conversation_id")
        if not str(payload.get("content") or "").strip():
            errors.append("Missing required field: content")
        if not (payload.get("event_at") or payload.get("created_at") or payload.get("ts")):
            errors.append("Missing required field: event_at (or created_at / ts)")
        if errors:
            return ValidationResult(is_valid=False, errors=errors, metadata={})
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id
