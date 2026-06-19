"""Wiki MVP Phase 5: topic_clusters + topic_cluster_members (memory_topic_map)."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_phase5_topic_clusters_v1"


def apply_wiki_mvp_phase5_topic_clusters_up(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS topic_clusters (
            cluster_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            dimension TEXT NOT NULL DEFAULT 'memory',
            member_count INTEGER NOT NULL DEFAULT 0,
            source_mix_json TEXT NOT NULL DEFAULT '{}',
            label_terms_json TEXT NOT NULL DEFAULT '[]',
            centroid_preview TEXT,
            model TEXT,
            provider TEXT,
            sync_batch_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_topic_clusters_dimension ON topic_clusters(dimension);

        CREATE TABLE IF NOT EXISTS topic_cluster_members (
            member_id TEXT PRIMARY KEY,
            cluster_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            record_type TEXT NOT NULL DEFAULT 'unknown',
            text_preview TEXT,
            weight REAL NOT NULL DEFAULT 1.0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(cluster_id, record_id, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_topic_cluster_members_cluster ON topic_cluster_members(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_topic_cluster_members_source ON topic_cluster_members(source_id, record_id);
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_mvp_phase5_topic_clusters_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS topic_cluster_members;
        DROP TABLE IF EXISTS topic_clusters;
        DELETE FROM wiki_schema_migrations WHERE migration_id=?;
        """,
        (MIGRATION_ID,),
    )
    conn.commit()
