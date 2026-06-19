"""Person model tables for cross-source identity (remediation PRD_04)."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "remediation_person_model_v1"


def apply_remediation_person_model_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS persons (
            person_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            display_name TEXT,
            is_owner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS person_aliases (
            alias_id TEXT PRIMARY KEY,
            person_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            alias_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(owner_user_id, alias_type, alias_value)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_person_aliases_lookup
        ON person_aliases(owner_user_id, alias_type, alias_value)
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO wiki_schema_migrations (migration_id, applied_at)
        VALUES (?, datetime('now'))
        """,
        (MIGRATION_ID,),
    )
    conn.commit()
