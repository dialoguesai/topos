"""Shared Data Explorer table taxonomy (layer classification helpers)."""

from __future__ import annotations

# Owner permission state: protection clock, owner attestations, ingest provenance.
# These rows are the owner's own restrictions and consent record, not content, and
# the legacy inspection handlers (get_table_rows, list_database_tables and the
# explorer) serve any table to a non-owner principal while no black hole is
# active. Reading them would disclose which records the owner put Off-limits and
# which facts they excluded, so they are hidden from every explorer surface and
# can never be dropped or cleared there.
PERMISSION_STATE_TABLE_PREFIXES: tuple[str, ...] = ("permissions_v2_", "ingest_provenance_")


def is_permission_state_table(table_name: str) -> bool:
    return str(table_name or "").startswith(PERMISSION_STATE_TABLE_PREFIXES)


# Operational tables served only to the owner's own surface (owner_app). The
# legacy inspection handlers answer third-party MCP clients (behind the owner's
# MCP policy), routines, and unstamped relay calls, and none of those may read
# these rows:
#
# - pipeline_jobs: payload_json carried the node's engine key
#   (progress_api_key) and caller-supplied Signal SQLCipher keys until
#   job_secrets withheld them, and rows written before that stay until the
#   startup scrub runs. It still holds raw import bodies (file_base64,
#   canonical_records) that no record protection filters.
# - mcp_clients: the token verifier (token_hash) of every enrolled MCP client.
#   Its own enroll/list/revoke handlers are already owner-only; the raw table
#   was the way around them.
#
# Unlike the permission-state tables, the owner can still inspect these.
OWNER_ONLY_TABLES: frozenset[str] = frozenset({"pipeline_jobs", "mcp_clients"})


def is_owner_only_table(table_name: str) -> bool:
    return str(table_name or "").strip() in OWNER_ONLY_TABLES


def hidden_from_current_principal(table_name: str) -> bool:
    """True when ``table_name`` must not be served to whoever is asking.

    Reads the channel-verified principal the dispatcher scoped for this request.
    Anything short of owner_app is refused, including no principal at all, the
    same way the dispatcher's owner-only gates treat it.
    """
    if not is_owner_only_table(table_name):
        return False
    from .principal import OWNER_APP, current_principal

    return getattr(current_principal(), "cls", None) != OWNER_APP


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
        "transcripts",
        "transcript_speakers",
        "transcript_segments",
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
    "transcripts": "transcript_id",
    "transcript_speakers": "speaker_id",
    "transcript_segments": "segment_id",
}


def is_canonical_schema_table(table_name: str) -> bool:
    return str(table_name or "").strip() in CANONICAL_SCHEMA_TABLES
