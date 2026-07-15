"""B1: calendar_events gains an availability layer + a value/movability layer
(PLAN_CANONICAL_CALENDAR_DOCUMENTS Part B).

*Availability layer* (raw, from Google Calendar): is_busy, status, is_all_day,
self_response_status, is_organizer, is_recurring, event_type, timezone,
location, description, url, attendee_count, accepted_count, created_at,
updated_at. These let free/busy and availability queries filter in SQL
instead of digging through metadata_json (the demo mapper's is_busy has
always been metadata-only and never consumed by anything downstream).

*Value layer* (derived): attendance_priority, movability_score, value_score,
value_reason, priority_confidence. Enrichment jobs write derived data to side
tables (signal_scores, …), never new canonical columns — so a value that
needs to be inline & queryable on calendar_events (for pricing/swap logic)
has to be computed by the mapper at map time, which is what
GoogleCalendarMapper does.

Plus an index on (starts_at, ends_at) so interval/overlap queries don't full
table scan.

Idempotent: the column adds are PRAGMA-guarded and re-run cheaply after the
legacy ``CREATE TABLE IF NOT EXISTS calendar_events`` DDL (same pattern as
activity_events_content_v1 / canonical_disclosure_v1), so this migration is
registered in BOTH apply_all_migrations and ensure_migrations_applied and
does not gate its column adds on the migration ledger. No backfill:
pre-existing rows keep NULL, which every reader must treat as "unknown" (not
"busy" or "free").
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "calendar_events_scheduling_v1"

_CALENDAR_COLUMNS = (
    # Availability layer (raw, from Google Calendar).
    ("is_busy", "INTEGER"),
    ("status", "TEXT"),
    ("is_all_day", "INTEGER"),
    ("self_response_status", "TEXT"),
    ("is_organizer", "INTEGER"),
    ("is_recurring", "INTEGER"),
    ("event_type", "TEXT"),
    ("timezone", "TEXT"),
    ("location", "TEXT"),
    ("description", "TEXT"),
    ("url", "TEXT"),
    ("attendee_count", "INTEGER"),
    ("accepted_count", "INTEGER"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    # Value layer (derived, computed by the mapper — inline so pricing/swap
    # logic can read it straight off the row).
    ("attendance_priority", "TEXT"),
    ("movability_score", "REAL"),
    ("value_score", "REAL"),
    ("value_reason", "TEXT"),
    ("priority_confidence", "REAL"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def apply_calendar_events_scheduling_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for column, col_type in _CALENDAR_COLUMNS:
        _add_column_if_missing(conn, "calendar_events", column, col_type)
    if _table_exists(conn, "calendar_events"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_events_window "
            "ON calendar_events(starts_at, ends_at)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
