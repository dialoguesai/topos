"""Unified timeline projection (P6): one temporal index across canonical tables."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_timeline_v1"


def apply_wiki_timeline_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS timeline (
            event_at TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            record_type TEXT,
            entity_ids_json TEXT NOT NULL DEFAULT '[]',
            cluster_id TEXT,
            signal_dimension TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (event_at, record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_table ON timeline(canonical_table, event_at);
        CREATE INDEX IF NOT EXISTS idx_timeline_record ON timeline(record_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_timeline_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS timeline")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
