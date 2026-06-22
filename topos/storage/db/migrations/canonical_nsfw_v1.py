"""Ingest-time NSFW content tags on canonical message tables (Platform Privacy Layer)."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "canonical_nsfw_v1"

_NSFW_COLUMNS: tuple[str, ...] = (
    "content_nsfw",
    "content_nsfw_score",
    "content_nsfw_model",
)

_NSFW_TABLES: tuple[str, ...] = (
    "ai_chat_messages",
    "conversation_messages",
    "journal_entries",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply_canonical_nsfw_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for table in _NSFW_TABLES:
        _add_column_if_missing(conn, table, "content_nsfw", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, table, "content_nsfw_score", "REAL")
        _add_column_if_missing(conn, table, "content_nsfw_model", "TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
