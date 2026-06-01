"""Canonical mappers for messenger sources (imessage, signal).

Produce the shape expected by conversation_messages. The ingestion pipeline
routes canonical_group_id=conversations to ConversationsTablesManager and
builds staging records from normalized payloads directly; these mappers
are for registry completeness and any code that looks up imessage/signal by mapper_id.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


def _normalized_to_canonical_payload(normalized: NormalizedRecord, source_id: str) -> dict:
    """Convert normalized messenger record to canonical message shape for conversation_messages."""
    p = normalized.payload
    payload = {
        "message_id": p.get("message_id", normalized.record_id),
        "conversation_id": p.get("thread_id") or p.get("conversation_id") or "",
        "sender_type": p.get("sender_type", "human"),
        "sender_id": p.get("sender_id"),
        "reply_to_message_id": p.get("reply_to_message_id"),
        "message_type": p.get("message_type"),
        "event_type": p.get("event_type"),
        "content": p.get("content", ""),
        "ts": p.get("ts", ""),
        "source_id": source_id,
    }
    if "_metadata" in p:
        payload["_metadata"] = p["_metadata"]
    return payload


@dataclass
class ImessageCanonicalMapper(CanonicalMapper):
    """Maps normalized iMessage record to canonical shape for conversation_messages."""

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = _normalized_to_canonical_payload(normalized, "imessage")
        return CanonicalRecord(record_id=payload["message_id"], payload=payload)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="imessage", mapping_version="v1")


@dataclass
class SignalCanonicalMapper(CanonicalMapper):
    """Maps normalized Signal record to canonical shape for conversation_messages."""

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = _normalized_to_canonical_payload(normalized, "signal")
        return CanonicalRecord(record_id=payload["message_id"], payload=payload)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="signal", mapping_version="v1")
