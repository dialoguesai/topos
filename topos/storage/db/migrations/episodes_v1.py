"""B2.4: minimal episode/provenance object — one row per canonical ingest pass.

Formalizes what today is scattered across ``sync_batch_id`` stamps and
``extraction_artifacts``: each canonical batch that flows through
``run_post_canonical_pipeline`` records one episode with the source, batch,
timing, record count, and (best-effort, nullable until sibling work lands)
the source posture and the batch's provenance-role mix.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "episodes_v1"


def apply_episodes_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            episode_id TEXT PRIMARY KEY,
            source_id TEXT,
            sync_batch_id TEXT,
            dataset_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            n_records INTEGER NOT NULL DEFAULT 0,
            posture TEXT,
            role_mix_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_episodes_source
            ON episodes(source_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_sync_batch
            ON episodes(sync_batch_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
