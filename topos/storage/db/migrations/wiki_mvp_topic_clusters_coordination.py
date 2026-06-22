"""Topic cluster coordination metadata column."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_topic_clusters_coordination_v1"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def apply_wiki_mvp_topic_clusters_coordination_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cols = _table_columns(conn, "topic_clusters")
    if "metadata_json" not in cols:
        conn.execute(
            "ALTER TABLE topic_clusters ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
    if not conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone():
        conn.execute(
            "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
    conn.commit()
