"""Explicit signal IA: typed signal_objects table."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "explicit_signal_ia_signal_objects_v1"


def apply_signal_objects_up(conn: sqlite3.Connection) -> None:
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

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_objects (
            object_id TEXT PRIMARY KEY,
            signal_dimension TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            extractor_version TEXT NOT NULL DEFAULT 'v1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'system'
        );
        CREATE INDEX IF NOT EXISTS idx_signal_objects_dimension_type
            ON signal_objects(signal_dimension, object_type, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_objects_active_key
            ON signal_objects(signal_dimension, object_type, object_key)
            WHERE valid_to IS NULL;
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
