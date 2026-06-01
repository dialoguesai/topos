"""
Stage 9 schema registry: machine-readable contract for table/column canonical names,
types, and categories (informational vs organizational).

Source of truth: docs/SCHEMA_CONVENTIONS.md §7.
Used by: engine, control plane, and UI (via assist APIs).
Organizational columns are non-filterable by default.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# -----------------------------------------------------------------------------
# Column entry: one row per (table, column) with current DB name, canonical name, type, category.
# For Stage 9 rename targets, current_column_name != canonical_column_name (current = name in DB today).
# -----------------------------------------------------------------------------

CATEGORY_INFORMATIONAL = "informational"
CATEGORY_ORGANIZATIONAL = "organizational"


def _row(
    table: str,
    current: str,
    canonical: str,
    type_name: str,
    category: str,
) -> Dict[str, Any]:
    """Build a registry row; rename_target True when current != canonical."""
    return {
        "table_name": table,
        "current_column_name": current,
        "canonical_column_name": canonical,
        "type": type_name,
        "category": category,
        "filterable_by_default": category == CATEGORY_INFORMATIONAL,
        "rename_target": current != canonical,
    }


# Registry: all Stage 9 mapped tables from SCHEMA_CONVENTIONS.md §7.1–§7.7.
# §7.0 rename targets: current = name in DB today; canonical = name after migration.
SCHEMA_REGISTRY: List[Dict[str, Any]] = [
    # ----- conversation_messages (7.1) -----
    _row("conversation_messages", "message_id", "message_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "conversation_id", "conversation_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "sender_type", "sender_type", "text", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "sender_id", "sender_id", "text", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "reply_to_message_id", "reply_to_message_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "message_type", "message_type", "text", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "event_type", "event_type", "text", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "content", "content", "text", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "ts", "event_at", "timestamp_utc", CATEGORY_INFORMATIONAL),  # rename
    _row("conversation_messages", "source_id", "source_id", "text", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "metadata_json", "metadata_json", "json", CATEGORY_INFORMATIONAL),
    _row("conversation_messages", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("conversation_messages", "from_self", "is_from_self", "integer", CATEGORY_ORGANIZATIONAL),  # rename
    _row("conversation_messages", "owner_user_id", "owner_user_id", "identifier", CATEGORY_ORGANIZATIONAL),
    # ----- ai_chat_messages (7.2) -----
    _row("ai_chat_messages", "message_id", "message_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("ai_chat_messages", "conversation_id", "conversation_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("ai_chat_messages", "sender_type", "sender_type", "text", CATEGORY_INFORMATIONAL),
    _row("ai_chat_messages", "sender_id", "sender_id", "text", CATEGORY_INFORMATIONAL),
    _row("ai_chat_messages", "ts", "event_at", "timestamp_utc", CATEGORY_INFORMATIONAL),  # rename
    _row("ai_chat_messages", "content", "content", "text", CATEGORY_INFORMATIONAL),
    _row("ai_chat_messages", "content_rendered", "content_rendered", "text", CATEGORY_INFORMATIONAL),
    _row("ai_chat_messages", "metadata_json", "metadata_json", "json", CATEGORY_INFORMATIONAL),
    _row("ai_chat_messages", "seq", "sequence", "integer", CATEGORY_ORGANIZATIONAL),  # rename
    _row("ai_chat_messages", "source_id", "source_id", "text", CATEGORY_ORGANIZATIONAL),
    # ----- ai_chat_conversations (7.3) -----
    _row("ai_chat_conversations", "conversation_id", "conversation_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("ai_chat_conversations", "owner_user_id", "owner_user_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("ai_chat_conversations", "title", "title", "text", CATEGORY_INFORMATIONAL),
    _row("ai_chat_conversations", "source", "source_id", "text", CATEGORY_ORGANIZATIONAL),  # rename
    _row("ai_chat_conversations", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("ai_chat_conversations", "updated_at", "updated_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    # ----- browser_visits (7.4) -----
    _row("browser_visits", "record_id", "record_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "url", "url", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "visited_at", "visited_at", "timestamp_utc", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "title", "title", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "favicon_url", "favicon_url", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "hostname", "hostname", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "device_name", "device_name", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "tab_id", "tab_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "window_id", "window_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "incognito", "incognito", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "transition_type", "transition_type", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "pinned", "pinned", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "audible", "audible", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "muted", "muted", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "opener_tab_id", "opener_tab_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_visits", "referred_by", "referred_by", "text", CATEGORY_INFORMATIONAL),
    _row("browser_visits", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    # ----- browser_events (7.5) -----
    _row("browser_events", "record_id", "record_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "event_type", "event_type", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "url", "url", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "visited_at", "visited_at", "timestamp_utc", CATEGORY_INFORMATIONAL),
    _row("browser_events", "title", "title", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "favicon_url", "favicon_url", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "hostname", "hostname", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "device_name", "device_name", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "transition_type", "transition_type", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "content", "content", "text", CATEGORY_INFORMATIONAL),
    _row("browser_events", "tab_id", "tab_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "window_id", "window_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "incognito", "incognito", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "pinned", "pinned", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "audible", "audible", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "muted", "muted", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "opener_tab_id", "opener_tab_id", "integer", CATEGORY_ORGANIZATIONAL),
    _row("browser_events", "starred_at", "starred_at", "timestamp_utc", CATEGORY_INFORMATIONAL),
    _row("browser_events", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    # ----- browser_url_classification (7.6) -----
    _row("browser_url_classification", "source_table", "enriched_from_table", "identifier", CATEGORY_ORGANIZATIONAL),  # rename
    _row("browser_url_classification", "record_id", "record_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_url_classification", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("browser_url_classification", "url", "url", "text", CATEGORY_INFORMATIONAL),
    _row("browser_url_classification", "title", "title", "text", CATEGORY_INFORMATIONAL),
    _row("browser_url_classification", "url_category", "url_category", "text", CATEGORY_INFORMATIONAL),
    _row("browser_url_classification", "url_confidence", "url_confidence", "real", CATEGORY_INFORMATIONAL),
    _row("browser_url_classification", "model_name", "model_name", "text", CATEGORY_ORGANIZATIONAL),
    _row("browser_url_classification", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("browser_url_classification", "updated_at", "updated_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    # ----- message_emotions (7.7) -----
    _row("message_emotions", "message_id", "message_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("message_emotions", "source_id", "source_id", "text", CATEGORY_ORGANIZATIONAL),
    _row("message_emotions", "emotion_label", "emotion_label", "text", CATEGORY_INFORMATIONAL),
    _row("message_emotions", "confidence", "confidence", "real", CATEGORY_INFORMATIONAL),
    _row("message_emotions", "model", "model_name", "text", CATEGORY_ORGANIZATIONAL),  # rename
    _row("message_emotions", "all_emotions", "all_emotions_json", "json", CATEGORY_INFORMATIONAL),  # rename
    _row("message_emotions", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    # ----- contacts (Stage 11: contacts:resolve — canonical messenger address book) -----
    # See topos/storage/canonical/conversations_tables.py CREATE TABLE contacts / contact_identifiers
    _row("contacts", "contact_id", "contact_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "source_id", "source_id", "text", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "display_name", "display_name", "text", CATEGORY_INFORMATIONAL),
    _row("contacts", "known_usernames_json", "known_usernames_json", "json", CATEGORY_INFORMATIONAL),
    _row("contacts", "is_self", "is_self", "integer", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "last_import_source", "last_import_source", "text", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "last_import_run_id", "last_import_run_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "last_imported_at", "last_imported_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "sharing_policy_json", "sharing_policy_json", "json", CATEGORY_INFORMATIONAL),
    _row("contacts", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("contacts", "updated_at", "updated_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("contact_identifiers", "dataset_id", "dataset_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("contact_identifiers", "source_id", "source_id", "text", CATEGORY_ORGANIZATIONAL),
    _row("contact_identifiers", "identifier", "identifier", "text", CATEGORY_INFORMATIONAL),
    _row("contact_identifiers", "identifier_type", "identifier_type", "text", CATEGORY_INFORMATIONAL),
    _row("contact_identifiers", "contact_id", "contact_id", "identifier", CATEGORY_ORGANIZATIONAL),
    _row("contact_identifiers", "created_at", "created_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
    _row("contact_identifiers", "updated_at", "updated_at", "timestamp_utc", CATEGORY_ORGANIZATIONAL),
]

# Tables covered by this registry (for validation and iteration).
STAGE_9_TABLE_NAMES = [
    "conversation_messages",
    "ai_chat_messages",
    "ai_chat_conversations",
    "browser_visits",
    "browser_events",
    "browser_url_classification",
    "message_emotions",
    "contacts",
    "contact_identifiers",
]


def get_columns_for_table(
    table_name: str,
    *,
    include_organizational: bool = True,
    use_canonical_names: bool = False,
) -> List[Dict[str, Any]]:
    """
    Return column entries for a table. Used by assist APIs and UI.

    include_organizational: If False, return only informational columns (default filterable set).
    use_canonical_names: If True, return canonical_column_name as the column name to display/use;
        if False, return current_column_name (name in DB today).
    """
    rows = [r for r in SCHEMA_REGISTRY if r["table_name"] == table_name]
    if not include_organizational:
        rows = [r for r in rows if r["category"] == CATEGORY_INFORMATIONAL]
    if use_canonical_names:
        return [{**r, "column_name": r["canonical_column_name"]} for r in rows]
    return [{**r, "column_name": r["current_column_name"]} for r in rows]


def get_informational_columns(table_name: str) -> List[Dict[str, Any]]:
    """Return only informational (filterable-by-default) columns for a table."""
    return get_columns_for_table(table_name, include_organizational=False)


def get_rename_targets() -> List[Dict[str, Any]]:
    """Return all columns that are Stage 9 migration rename targets (current != canonical)."""
    return [r for r in SCHEMA_REGISTRY if r["rename_target"]]


def get_registry_as_list(
    *,
    include_organizational: bool = True,
) -> List[Dict[str, Any]]:
    """Return full registry as list of dicts (e.g. for JSON export or API)."""
    if include_organizational:
        return list(SCHEMA_REGISTRY)
    return [r for r in SCHEMA_REGISTRY if r["category"] == CATEGORY_INFORMATIONAL]


def get_table_names() -> List[str]:
    """Return list of table names in the registry."""
    return list(STAGE_9_TABLE_NAMES)


def resolve_column_to_canonical(table_name: str, current_column_name: str) -> Optional[str]:
    """
    Given a table and the current (DB) column name, return the canonical name.
    If not in registry or no rename, returns current_column_name (or None if unknown).
    """
    for r in SCHEMA_REGISTRY:
        if r["table_name"] == table_name and r["current_column_name"] == current_column_name:
            return r["canonical_column_name"]
    return None
