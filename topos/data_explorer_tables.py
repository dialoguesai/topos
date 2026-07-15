"""Shared Data Explorer table taxonomy (layer classification helpers)."""

from __future__ import annotations

# MVP canonical schema tables: fixed DDL from migrations — clear rows, never DROP.
CANONICAL_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "ai_chat_messages",
        "ai_chat_conversations",
        "ai_chat_participants",
        "conversation_messages",
        "conversations",
        "activity_events",
        "calendar_events",
        "contacts",
        "contact_identifiers",
        "journal_entries",
        "profile_records",
        "financial_transactions",
        "location_events",
        "documents",
    }
)


def is_canonical_schema_table(table_name: str) -> bool:
    return str(table_name or "").strip() in CANONICAL_SCHEMA_TABLES
