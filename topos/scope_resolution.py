# Scope → canonical table resolution (Sprint 05 Stage 1)
# Maps MVP scope IDs from RPT to canonical table names. Reference: roles_scopes/MVP_TAXONOMY.md §4

from __future__ import annotations

from typing import List, Optional, Set

# Scope ID -> canonical table(s). One scope can map to one or more tables.
# Table names match MVP_TAXONOMY §4 (messages, ai_messages, events, ai_chat, activity, journal, Profile).
SCOPE_TO_TABLES: dict[str, list[str]] = {
    "messages:read": ["messages"],
    "messages:write": ["messages"],
    "aiMessages:read": ["ai_messages"],
    "events:read": ["events"],
    "events:write": ["events"],
    "aiChat:read": ["ai_chat"],
    "activity:read": ["activity"],
    "activity:write": ["activity"],
    "journal:read": ["journal"],
    "journal:write": ["journal"],
    "activitySummary:read": ["activity_summary"],
    "wellnessSummary:read": ["wellness_summary"],
    "publicBio:read": ["public_bio"],
    "contacts:resolve": ["contacts", "contact_identifiers"],
    "all:read": ["*"],
    "all:write": ["*"],
}

# All canonical table names (for "all" resolution). Order doesn't matter.
# DO NOT add "documents" here: it is an owner-only leaf lane and must stay
# unreachable even by a guardian's all:read grant (owner-only, no sharing).
ALL_CANONICAL_TABLES: set[str] = {
    "messages",
    "ai_messages",
    "events",
    "ai_chat",
    "activity",
    "journal",
    "activity_summary",
    "wellness_summary",
    "public_bio",
    "contacts",
    "contact_identifiers",
}


def resolve_scopes_to_tables(scope_ids: Optional[List[str]]) -> Set[str]:
    """
    Map MVP scope IDs to canonical table names.
    If all:read or all:write is in scope_ids, returns ALL_CANONICAL_TABLES.
    Otherwise returns the union of tables for each scope in scope_ids.
    """
    if not scope_ids:
        return set()
    tables: Set[str] = set()
    for s in scope_ids:
        s = (s or "").strip()
        if not s:
            continue
        if s in ("all:read", "all:write"):
            return set(ALL_CANONICAL_TABLES)
        if s in SCOPE_TO_TABLES:
            mapped = SCOPE_TO_TABLES[s]
            if "*" in mapped:
                return set(ALL_CANONICAL_TABLES)
            tables.update(mapped)
    return tables


def may_access_table(allowed_tables: Set[str], table_or_alias: str) -> bool:
    """
    True if the allowed set (from resolve_scopes_to_tables) permits access to the given table.
    table_or_alias can be canonical name (e.g. messages) or implementation table (e.g. ai_chat_messages).
    """
    if not allowed_tables:
        return False
    if "*" in allowed_tables or "all" in allowed_tables:
        return True
    # Exact match
    if table_or_alias in allowed_tables:
        return True
    if table_or_alias == "conversation_messages" and "messages" in allowed_tables:
        return True
    # Implementation detail: ai_chat_messages table holds ai_chat and ai_messages data
    if table_or_alias == "ai_chat_messages" and ("ai_chat" in allowed_tables or "ai_messages" in allowed_tables):
        return True
    if table_or_alias == "messages" and "messages" in allowed_tables:
        return True
    if table_or_alias in ("contacts", "contact_identifiers"):
        return "contacts" in allowed_tables and "contact_identifiers" in allowed_tables
    return False
