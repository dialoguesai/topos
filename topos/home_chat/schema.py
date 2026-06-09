"""SQLite DDL for home chat sessions (functional feature storage, not canonical AIChat)."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("topos.home_chat.schema")


def ensure_home_chat_schema(conn: sqlite3.Connection) -> None:
    """Create home chat session table and indices if missing."""
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS home_chat_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                engine_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New chat',
                trace_session_id TEXT NOT NULL DEFAULT '',
                history_json TEXT NOT NULL DEFAULT '{}',
                participants_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at_ms INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_home_chat_sessions_user_engine_updated
            ON home_chat_sessions (user_id, engine_id, updated_at_ms DESC)
            """
        )
        _ensure_participants_column(conn)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_home_chat_schema failed: %s", exc)
        raise


def _ensure_participants_column(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(home_chat_sessions)")
    columns = {str(row[1]) for row in cur.fetchall()}
    if "participants_json" not in columns:
        conn.execute(
            "ALTER TABLE home_chat_sessions ADD COLUMN participants_json TEXT NOT NULL DEFAULT '[]'"
        )
