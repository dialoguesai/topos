"""Owner labels on latent affinity pairs (usable/tunable affinity UX).

Stores human verdicts so the People review queue and EntityDrawer can hide
already-labeled pairs and so too-few / too-many nudges can be judged against
real feedback rather than only edge counts.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "affinity_pair_labels_v1"


def apply_affinity_pair_labels_v1_up(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS affinity_pair_labels (
            label_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            cosine REAL,
            label TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (label IN ('useful', 'obvious', 'wrong', 'same_person')),
            UNIQUE (src_entity_id, dst_entity_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_affinity_pair_labels_created
        ON affinity_pair_labels(created_at)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_affinity_pair_labels_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS affinity_pair_labels")
    conn.execute(
        "DELETE FROM wiki_schema_migrations WHERE migration_id = ?",
        (MIGRATION_ID,),
    )
    conn.commit()
