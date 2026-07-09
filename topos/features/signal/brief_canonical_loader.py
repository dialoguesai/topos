"""Load canonical row text for dimension brief LLM refresh."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .dimension_registry import DIMENSION_CANONICAL_TABLES, is_signal_dimension

# Tables that exist or are planned; id column + primary text column for brief input.
_TABLE_TEXT_COLUMNS: Dict[str, Tuple[str, str]] = {
    "ai_chat_messages": ("message_id", "content"),
    "conversation_messages": ("message_id", "content"),
    "journal_entries": ("entry_id", "content"),
    "profile_records": ("record_id", "description"),
    "financial_transactions": ("transaction_id", "description"),
    "location_events": ("event_id", "place_name"),
    "activity_events": ("event_id", "title"),
    "calendar_events": ("event_id", "title"),
    "user_goals": ("goal_id", "goal_text"),
    "relationship_edges": ("edge_id", "payload_json"),
    "contacts": ("contact_id", "display_name"),
    "contact_identifiers": ("contact_id", "identifier"),
    "sleep_session": ("session_id", "summary"),
    "mood_entry": ("mood_id", "notes"),
    "activity_log": ("log_id", "activity_type"),
    "transaction": ("transaction_id", "description"),
    "receipt": ("receipt_id", "merchant_name"),
    "subscription": ("subscription_id", "service_name"),
    "financial_summary": ("summary_id", "summary_text"),
    "location_event": ("event_id", "place_label"),
    "trip": ("trip_id", "destination_label"),
    "place": ("place_id", "label"),
    "project": ("project_id", "name"),
    "task": ("task_id", "title"),
    "document": ("document_id", "title"),
    "note": ("note_id", "content"),
    "public_bio": ("bio_id", "bio_text"),
    "personal_profile": ("profile_id", "summary"),
    "skill": ("skill_id", "label"),
    "experience": ("experience_id", "title"),
}


# P1.3 (PLAN_PROVENANCE_SPLIT): brief inputs must know WHO said each row so
# brief_input_text can label non-authored speech ("[contact] …"/"[assistant] …")
# instead of letting anyone's words read as the owner's. These are the sender
# truth columns per message family (Appendix A).
_TABLE_SENDER_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "ai_chat_messages": ("sender_type",),
    "conversation_messages": ("sender_type", "sender_id", "is_from_self"),
}


def _text_columns_for_table(table: str, dimension: str) -> Tuple[str, str]:
    if table == "journal_entries" and dimension == "places":
        return ("entry_id", "place_name")
    return _TABLE_TEXT_COLUMNS[table]


def load_canonical_messages_for_dimension(
    conn: sqlite3.Connection,
    dimension: str,
    *,
    limit: int = 40,
    source_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Gather recent text-bearing canonical rows for a dimension's mapped tables."""
    dim = str(dimension or "").strip().lower()
    if not is_signal_dimension(dim):
        return []

    per_table = max(1, min(int(limit), 50))
    messages: List[Dict[str, Any]] = []
    seen_tables = set()
    sid_filter = str(source_id or "").strip()

    for table in DIMENSION_CANONICAL_TABLES.get(dim, []):
        if table in seen_tables:
            continue
        seen_tables.add(table)
        cols = _text_columns_for_table(table, dim)
        if table not in _TABLE_TEXT_COLUMNS:
            continue
        id_col, text_col = cols
        sender_cols = _TABLE_SENDER_COLUMNS.get(table, ())
        select_cols = ", ".join((id_col, text_col, "source_id", *sender_cols))
        try:
            if sid_filter:
                rows = conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM {table}
                    WHERE source_id = ?
                      AND {text_col} IS NOT NULL AND TRIM({text_col}) != ''
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (sid_filter, per_table),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM {table}
                    WHERE {text_col} IS NOT NULL AND TRIM({text_col}) != ''
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (per_table,),
                ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            content = str(row[1] or "").strip()
            if not content:
                continue
            record: Dict[str, Any] = {
                "message_id": row[0],
                "record_id": row[0],
                "content": content,
                "source_id": row[2],
                "canonical_table": table,
            }
            for offset, sender_col in enumerate(sender_cols):
                record[sender_col] = row[3 + offset]
            if table == "journal_entries" and dim == "places":
                record["place_name"] = content
            messages.append(record)

    return messages[: max(1, min(int(limit), 100))]
