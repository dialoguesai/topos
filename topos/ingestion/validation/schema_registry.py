"""Minimal schema registry for ingestion validation."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.ingestion.schema_registry")

SCHEMAS: Dict[str, Dict[str, Any]] = {
    "chatgpt.conversation.v1": {
        "name": "ChatGPT Conversation Export",
        "version": "1",
        "required_fields": ["id", "thread_id", "role", "content", "created_at"],
        "field_types": {
            "id": str,
            "thread_id": str,
            "role": str,
            "content": str,
            "created_at": (int, float, str),
        },
        "description": "ChatGPT conversation export format (JSONL - flat records)",
        "file_format": "jsonl",
    },
    "chatgpt.conversation.v2": {
        "name": "ChatGPT Real Export Format",
        "version": "2",
        "required_fields": ["id", "thread_id", "role", "content", "created_at"],
        "field_types": {
            "id": str,
            "thread_id": str,
            "role": str,
            "content": str,
            "created_at": (int, float, str),
        },
        "description": "ChatGPT real export format (JSON array of conversation objects, flattened to v1 format)",
        "file_format": "json",
        "source_structure": "conversation_array",
        "note": "Flattened records match v1 format, but source is nested conversation objects",
    },
    # Sprint 02: Messenger ingestion (same logical shape as chat for conversation_messages)
    "imessage.messages.v1": {
        "name": "iMessage Messages",
        "version": "1",
        "required_fields": ["id", "thread_id", "role", "content", "created_at"],
        "field_types": {
            "id": str,
            "thread_id": str,
            "role": str,
            "content": str,
            "created_at": (int, float, str),
        },
        "description": "iMessage message format (normalized from chat.db or sync); id may be imessage:ROWID",
        "file_format": "jsonl",
    },
    "transcript.session.v1": {
        "name": "Transcript Session",
        "version": "1",
        "required_fields": ["transcript_id", "items"],
        "field_types": {
            "transcript_id": str,
            "title": str,
            "origin_url": str,
            "items": list,
        },
        "description": (
            "Session-shaped transcript (YouTube archive or meeting tool). "
            "items[] are caption utterances; role fields are ignored at parse."
        ),
        "file_format": "json",
    },
    "signal.messages.v1": {
        "name": "Signal Messages",
        "version": "1",
        "required_fields": ["id", "thread_id", "role", "content", "created_at"],
        "field_types": {
            "id": str,
            "thread_id": str,
            "role": str,
            "content": str,
            "created_at": (int, float, str),
        },
        "description": "Signal Desktop message format (normalized from SQLCipher or export); id may be signal:uuid",
        "file_format": "jsonl",
    },
}


def get_schema_definition(schema_id: str) -> Optional[Dict[str, Any]]:
    return SCHEMAS.get(schema_id)


def validate_schema(record: Dict[str, Any], schema_id: str) -> tuple[bool, Optional[str]]:
    schema = get_schema_definition(schema_id)
    if not schema:
        return False, f"Unknown schema: {schema_id}"

    required_fields = schema.get("required_fields", [])
    for field in required_fields:
        if field not in record:
            return False, f"Missing required field: {field}"

    field_types = schema.get("field_types", {})
    for field, expected_type in field_types.items():
        if field not in record:
            continue
        value = record[field]
        if expected_type is None:
            continue
        if isinstance(expected_type, tuple):
            if not any(isinstance(value, t) for t in expected_type):
                return False, (
                    f"Field '{field}' has invalid type. "
                    f"Expected one of {expected_type}, got {type(value).__name__}"
                )
        elif not isinstance(value, expected_type):
            return False, (
                f"Field '{field}' has invalid type. "
                f"Expected {expected_type.__name__}, got {type(value).__name__}"
            )

    return True, None


def register_schema(schema_id: str, schema_def: Dict[str, Any]) -> None:
    SCHEMAS[schema_id] = schema_def
    logger.info("Registered schema: %s", schema_id)
