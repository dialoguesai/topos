"""Canonical provenance shared by goal consumers; independent of any analytics view."""
from datetime import datetime, timezone
import sqlite3

def _instant(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def goal_sources(conn, *, goal_ids=None):
    """Date and attribute goals via canonical sources, never extraction or timeline time.

    Each source is independent so a missing optional table does not erase other sources.
    Unknown authorship stays unknown. An assistant's suggestion is not an owner goal.
    """
    # A row the owner keeps from every share (ai_chat_messages.share_excluded) is never cited as a
    # goal's evidence; an older database without the column is read as before.
    from ...storage.share_excluded import share_excluded_clause

    paths = (
        ("journal_entries", "j.entry_id = g.record_id OR j.source_record_id = g.record_id", "j.entry_at", "j.content", "1"),
        ("ai_chat_messages", "j.message_id = g.record_id" + share_excluded_clause(conn, alias="j"), "j.event_at", "j.content", "j.sender_type IN ('human','user','owner','self')"),
        ("conversation_messages", "j.message_id = g.record_id", "j.event_at", "j.content", "j.is_from_self = 1"),
    )
    if goal_ids is not None and not goal_ids:
        return []
    result = []
    seen = set()
    for table, join, at, content, owner in paths:
        try:
            thread = "j.conversation_id" if table != "journal_entries" else "g.record_id"
            restriction = "" if goal_ids is None else " AND g.goal_id IN (" + ",".join("?" for _ in goal_ids) + ")"
            rows = conn.execute(f"SELECT g.goal_id,g.record_id,g.goal_text,{at},{owner},{thread} FROM user_goals g JOIN {table} j ON {join} WHERE g.goal_text IS NOT NULL" + restriction, tuple(goal_ids or ())).fetchall()
        except sqlite3.Error:
            continue
        for gid, rid, text, when, authored, thread in rows:
            if gid in seen or not _instant(when):
                continue
            seen.add(gid)
            result.append(dict(goal_id=str(gid), record_id=str(rid), text=str(text),
                               event_at=str(when), source_table=table, source_thread=str(thread or rid),
                               owner_authored=bool(authored)))
    return result

