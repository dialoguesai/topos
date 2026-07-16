"""Canonical tables/fields that receive ingest-time PII disclosure columns."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# NOTE: the `documents` lane is intentionally absent from every map in this
# module. It is an owner-only leaf (store-and-view only) that is never disclosed
# or shared, so it does not participate in the disclosure/redaction pipeline.
CANONICAL_ID_COLUMN: Dict[str, str] = {
    "ai_chat_messages": "message_id",
    "conversation_messages": "message_id",
    "journal_entries": "entry_id",
}

# canonical_table -> raw fields redacted at ingest
PII_DISCLOSURE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "ai_chat_messages": ("content", "content_rendered"),
    "conversation_messages": ("content",),
    "journal_entries": ("content",),
}

_GROUP_TO_TABLE = {
    "ai_messages": "ai_chat_messages",
    "conversations": "conversation_messages",
    "journal": "journal_entries",
}


# Grantee-facing placeholder when a record's ingest disclosure has not completed.
# Read paths must emit this instead of raw content (fail closed). Kept in sync with
# the SQL fallback literal in storage/adapters/sqlite/stores.py disclosure specs.
DISCLOSURE_PENDING_PLACEHOLDER = "[disclosure pending]"

# Idempotence marker: set on a row once the grantee content policy has resolved it, so a
# second application is a no-op instead of re-treating already-redacted content as "pending"
# and overwriting it with the placeholder. Survives strip so re-application can see it.
DISCLOSURE_APPLIED_MARKER = "_disclosure_applied"


def disclosure_column(field: str) -> str:
    return f"{field}_disclosure"


def disclosure_hash_column(field: str) -> str:
    return f"{field}_disclosure_hash"


def canonical_table_for_group(group: Optional[str]) -> Optional[str]:
    if not group:
        return None
    return _GROUP_TO_TABLE.get(str(group).strip())


def canonical_table_for_message(msg: Dict[str, Any], *, source_group: Optional[str] = None) -> Optional[str]:
    explicit = msg.get("_table") or msg.get("canonical_table")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if source_group:
        return canonical_table_for_group(source_group)
    return None


def fields_for_table(table: str) -> Tuple[str, ...]:
    return PII_DISCLOSURE_FIELDS.get(table, ())
