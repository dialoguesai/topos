"""Mention-context centroids for entities (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.1).

A separate table, deliberately not a column on ``entities``. ``entities.embedding_blob``
holds a *canonical-name* embedding — a dedup signal already consumed by the
consolidation sweep — so affinity built on it would only ever rediscover aliases.
This table holds the average of the embeddings of the records an entity is
mentioned in: what the entity is talked *about*, not what it is called.

Layer-4 derived: rebuildable from Layers 2–3, and independently scrubbable
precisely because it is its own table.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_context_vectors_v1"


def apply_entity_context_vectors_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_context_vectors (
            entity_id TEXT PRIMARY KEY,
            centroid_blob BLOB,
            mention_sample INTEGER NOT NULL DEFAULT 0,
            model_name TEXT,
            computed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entity_context_vectors_computed
        ON entity_context_vectors(computed_at)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_context_vectors_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS entity_context_vectors")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
