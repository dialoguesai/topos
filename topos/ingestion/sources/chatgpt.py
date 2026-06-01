from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal

from .base import RawRecord, SourceConnector, SourceIdentity, SourcePayload


@dataclass
class ChatGPTSourceConnector(SourceConnector):
    source_name: str = "chatgpt"
    source_type: Literal["file", "sqlite"] = "file"

    def ingest(self, payload: SourcePayload) -> str:
        _ = payload
        return "chatgpt.conversation.v1"

    def schema(self) -> Dict[str, str]:
        return {"schema_id": "chatgpt.conversation.v1"}

    def identity(self, record: RawRecord) -> SourceIdentity:
        return SourceIdentity(
            source_system="chatgpt",
            source_record_id=record.record_id,
            source_export_id=record.record_id,
        )

    def canonical_eligible(self) -> bool:
        return True
