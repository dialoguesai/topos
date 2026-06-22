"""Wave A: scope/source generation watermark for retrieval fingerprint invalidation."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "scope_source_generation_v1"


def apply_scope_source_generation_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone():
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scope_source_generation (
            scope_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (scope_id, source_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
