"""Shared parser base for the AI coding-tool transcript records (CONTRACTS §2.1–2.2).

The readers (``ingestion/sources/<tool>_reader.py``) do the real work:
classification, scrub, taint. By the time a record reaches a parser it is a
§2.2 flat record, so a parser is a validator plus the same five-field
normalisation ``ChatGPTParser`` does — with ``sender_type`` taken from the
role (user→human, assistant→assistant, system→system) rather than collapsing
everything that is not the user into "assistant".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List

from ...log_preview import field_preview
from ...sources.base import RawRecord
from ...validation.base import ValidationResult
from ...validation.schema_registry import validate_schema
from ..base import NormalizedRecord, Parser
from .common import ROLES

logger = logging.getLogger("topos.ingestion.parser.ai_coding_tool")

SENDER_TYPE_BY_ROLE: Dict[str, str] = {"user": "human", "assistant": "assistant", "system": "system"}


def contract_errors(payload: Dict[str, Any], tool: str) -> List[str]:
    """Everything in a §2.2 record that the schema registry's type check cannot see."""
    errors: List[str] = []
    role = payload.get("role")
    if role not in ROLES:
        errors.append(f"role must be one of {list(ROLES)}, got {role!r}")
    record_id = payload.get("id")
    if not isinstance(record_id, str) or not record_id.startswith(f"{tool}:") or len(record_id) <= len(tool) + 1:
        errors.append(f"id must be '{tool}:<record_uuid>', got {record_id!r}")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        errors.append("content must be non-empty after stripping")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        errors.append("created_at must be an ISO 8601 string")
    metadata = payload.get("_metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append("_metadata must be an object")
        elif metadata.get("tool") not in (None, tool):
            errors.append(f"_metadata.tool must be {tool!r}, got {metadata.get('tool')!r}")
    return errors


@dataclass
class AICodingToolParser(Parser):
    """One subclass per tool; ``tool`` and ``_schema_id`` are the only differences."""

    dataset_id: str
    _schema_id: str = ""
    tool: ClassVar[str] = ""

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload
        role = str(payload.get("role") or "").lower()
        normalized: Dict[str, Any] = {
            "message_id": str(payload.get("id") or raw.record_id),
            "dataset_id": self.dataset_id,
            "thread_id": str(payload.get("thread_id") or ""),
            "ts": str(payload.get("created_at") or ""),
            "sender_type": SENDER_TYPE_BY_ROLE.get(role, "system"),
            "content": payload.get("content") or "",
        }
        # The reader's facets (title, turn_index, origin, share_excluded …);
        # the mapper merges them into metadata_json.
        if isinstance(payload.get("_metadata"), dict):
            normalized["_metadata"] = dict(payload["_metadata"])
        logger.debug(
            "[PIPELINE:PARSER] Parsed %s record: message_id=%s, sender_type=%s, thread_id=%s, content_preview=%s",
            self.tool,
            normalized["message_id"],
            normalized["sender_type"],
            normalized["thread_id"],
            field_preview(normalized.get("content")),
        )
        return NormalizedRecord(record_id=normalized["message_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        is_valid, error = validate_schema(record.payload, self.schema_id())
        errors = [] if is_valid else [error or "Invalid record"]
        if is_valid:
            errors = contract_errors(record.payload, self.tool)
        return ValidationResult(is_valid=not errors, errors=errors, metadata={})

    def schema_id(self) -> str:
        return self._schema_id
