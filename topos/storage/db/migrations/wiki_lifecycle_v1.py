"""Intelligence lifecycle support (P8): owner exclusion tombstones.

An exclusion is the owner saying "this must not be part of my intelligence".
Deleting a derived artifact alone is not enough — extraction is idempotent and
would resurrect it on the next batch. Tombstones are consulted by every
producer (fact assertion, stat promotion, entity resolution, dossier refresh).
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_lifecycle_v1"


def apply_wiki_lifecycle_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS intelligence_exclusions (
            exclusion_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,   -- fact | entity | stat_insight | record
            artifact_key TEXT NOT NULL,    -- fact: subj:predicate[:value] | entity: normalized name | stat: stat_id:group | record: record_id
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by TEXT NOT NULL DEFAULT 'owner'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_intelligence_exclusions_key
            ON intelligence_exclusions(artifact_type, artifact_key);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_lifecycle_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS intelligence_exclusions")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
