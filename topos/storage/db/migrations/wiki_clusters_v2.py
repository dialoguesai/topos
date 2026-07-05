"""Incremental clustering support (P5): candidate pool + recompute bookkeeping."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_clusters_v2"


def apply_wiki_clusters_v2_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cluster_candidates (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS cluster_recompute_log (
            recompute_id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            records_processed INTEGER,
            clusters_written INTEGER,
            ids_preserved INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_clusters_v2_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS cluster_recompute_log;
        DROP TABLE IF EXISTS cluster_candidates;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
