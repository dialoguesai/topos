"""Ingest-time PII disclosure columns on canonical message tables."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "canonical_disclosure_v1"

_DISCLOSURE_SPECS: dict[str, tuple[str, ...]] = {
    "ai_chat_messages": (
        "content_disclosure",
        "content_disclosure_hash",
        "content_rendered_disclosure",
        "content_rendered_disclosure_hash",
        "content_disclosure_model",
    ),
    "conversation_messages": (
        "content_disclosure",
        "content_disclosure_hash",
        "content_disclosure_model",
    ),
    "journal_entries": (
        "content_disclosure",
        "content_disclosure_hash",
        "content_disclosure_model",
    ),
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def apply_canonical_disclosure_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for table, columns in _DISCLOSURE_SPECS.items():
        for column in columns:
            _add_column_if_missing(conn, table, column)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
