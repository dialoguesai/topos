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

#: canonical table -> the column that UNIQUELY identifies one of its rows.
#:
#: Deliberately not ``storage.adapters.sqlite.stores._NATIVE_ID_COL``, which the
#: fan-out parent probe used to borrow: that map covers 10 of these 14 tables, so
#: a parent living in ``documents``, ``conversations`` or ``ai_chat_conversations``
#: was never detected and the destructive upstream delete survived for it.
#:
#: Tables with no single-column identity are absent on purpose rather than
#: approximated. ``contact_identifiers`` is keyed on
#: ``(dataset_id, source_id, identifier)`` and its ``contact_id`` is a non-unique
#: FK, so probing it returns a value that does not name a row;
#: ``ai_chat_participants`` declares no primary key at all. ``conversations`` is
#: keyed on ``(conversation_id, dataset_id)`` — ``conversation_id`` is included
#: because it is unique in practice per dataset and a false positive here only
#: NARROWS a delete, which is the safe direction.
CANONICAL_ROW_ID_COLUMN: dict[str, str] = {
    "ai_chat_messages": "message_id",
    "ai_chat_conversations": "conversation_id",
    "conversation_messages": "message_id",
    "conversations": "conversation_id",
    "activity_events": "event_id",
    "calendar_events": "event_id",
    "contacts": "contact_id",
    "journal_entries": "entry_id",
    "profile_records": "record_id",
    "financial_transactions": "transaction_id",
    "location_events": "event_id",
    "documents": "doc_id",
}


def is_canonical_schema_table(table_name: str) -> bool:
    return str(table_name or "").strip() in CANONICAL_SCHEMA_TABLES
