from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ...ingestion.parsers.base import NormalizedRecord
from ..models import CanonicalMessage
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


@dataclass
class ChatGPTCanonicalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = normalized.payload
        content = payload.get("content", "")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        message_id = payload.get("message_id", normalized.record_id)
        
        # Preserve _metadata for conversation tree reconstruction
        metadata = {"mapper_version": self.version}
        if "_metadata" in payload:
            # Merge _metadata into metadata (preserves parent_id, node_id, etc.)
            metadata.update(payload["_metadata"])
        
        canonical = CanonicalMessage(
            message_id=message_id,
            conversation_id=payload.get("thread_id", ""),
            sender_type=payload.get("sender_type", ""),
            content=content,
            ts=payload.get("ts"),
            source_id="chatgpt",
            content_hash=content_hash,
            metadata=metadata,
        )
        out = canonical.__dict__
        out["source_record_id"] = normalized.record_id
        return CanonicalRecord(record_id=canonical.message_id, payload=out)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="chatgpt", mapping_version=self.version)
