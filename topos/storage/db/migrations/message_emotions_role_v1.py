"""Persist actor role on message_emotions (Prov follow-up / emotion role stamp).

Emo27 already stamps ``role`` on enrichment records; the derived schema dropped it.
Additive column so wellbeing surfaces can filter authored(+addressed) vs observed.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "message_emotions_role_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_message_emotions_role_v1_up(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "message_emotions"):
        cols = _columns(conn, "message_emotions")
        if "role" not in cols:
            conn.execute("ALTER TABLE message_emotions ADD COLUMN role TEXT")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_message_emotions_role
               ON message_emotions(role) WHERE role IS NOT NULL"""
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
