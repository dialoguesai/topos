"""Add starts_at to journal_entries."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "journal_entries_starts_at_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _table_exists(conn, table):
        return
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {row[1] for row in rows}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply_journal_entries_starts_at_v1_up(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "journal_entries", "starts_at", "TEXT")
    if not _table_exists(conn, "journal_entries"):
        return
    conn.execute(
        """
        UPDATE journal_entries
        SET starts_at = entry_at
        WHERE (starts_at IS NULL OR starts_at = '')
          AND entry_at IS NOT NULL
          AND entry_at != ''
        """
    )
    conn.commit()
