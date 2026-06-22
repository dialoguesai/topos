"""Resolve canonical source text for vector record_ids."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class HydrationResult:
    record_id: str
    content: str
    found: bool
    table: Optional[str] = None
    source_id: Optional[str] = None
    truncated: bool = False
    error: Optional[str] = None


_TABLE_LOOKUPS = (
    ("ai_chat_messages", "message_id", ("content", "content_rendered")),
    ("conversation_messages", "message_id", ("content", "content_rendered", "body")),
    ("activity_events", "event_id", ("title", "url", "description")),
    ("journal_entries", "entry_id", ("content",)),
    ("profile_records", "record_id", ("title", "description", "organization")),
    ("calendar_events", "event_id", ("title", "description")),
    ("location_events", "event_id", ("place_name", "city", "event_type")),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _compose_fields(row: sqlite3.Row, fields: tuple[str, ...], *, disclosure_tier: str = "owner_raw") -> str:
    parts = []
    for field in fields:
        value = row[field] if field in row.keys() else None
        if disclosure_tier == "default_disclosure":
            disc_col = f"{field}_disclosure"
            if disc_col in row.keys() and row[disc_col]:
                value = row[disc_col]
        if value:
            parts.append(str(value).strip())
    return " — ".join(parts).strip()


def hydrate_record_text(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    source_id: Optional[str] = None,
    record_type: Optional[str] = None,
    max_chars: int = 2000,
    disclosure_tier: str = "owner_raw",
) -> HydrationResult:
    rid = str(record_id or "").strip()
    if not rid:
        return HydrationResult(record_id="", content="", found=False)

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _hydrate_record_text(
            conn, rid, source_id=source_id, record_type=record_type, max_chars=max_chars, disclosure_tier=disclosure_tier
        )
    finally:
        conn.row_factory = previous_factory


def _hydrate_record_text(
    conn: sqlite3.Connection,
    rid: str,
    *,
    source_id: Optional[str] = None,
    record_type: Optional[str] = None,
    max_chars: int = 2000,
    disclosure_tier: str = "owner_raw",
) -> HydrationResult:

    preferred_table = None
    if record_type == "ai_chat_message":
        preferred_table = "ai_chat_messages"
    elif record_type == "activity_event":
        preferred_table = "activity_events"
    elif record_type == "journal_entry":
        preferred_table = "journal_entries"

    tables = list(_TABLE_LOOKUPS)
    if preferred_table:
        tables = [t for t in tables if t[0] == preferred_table] + [t for t in tables if t[0] != preferred_table]

    for table, id_col, fields in tables:
        if not _table_exists(conn, table):
            continue
        query = f"SELECT * FROM {table} WHERE {id_col} = ? LIMIT 1"
        params: list[Any] = [rid]
        if source_id and "source_id" in {
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }:
            query = f"SELECT * FROM {table} WHERE {id_col} = ? AND source_id = ? LIMIT 1"
            params.append(source_id)
        try:
            row = conn.execute(query, tuple(params)).fetchone()
        except sqlite3.Error as exc:
            return HydrationResult(record_id=rid, content="", found=False, error=str(exc))
        if not row:
            continue
        content = _compose_fields(row, fields, disclosure_tier=disclosure_tier)
        if not content and table == "activity_events":
            content = " ".join(
                str(row[key] or "")
                for key in row.keys()
                if key in ("title", "url", "description")
            ).strip()
        if content:
            truncated = len(content) > max_chars
            return HydrationResult(
                record_id=rid,
                content=content[:max_chars],
                found=True,
                table=table,
                source_id=row["source_id"] if "source_id" in row.keys() else source_id,
                truncated=truncated,
            )

    return HydrationResult(record_id=rid, content="", found=False, source_id=source_id)
