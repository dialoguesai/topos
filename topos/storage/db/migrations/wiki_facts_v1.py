"""Fact store support (P4): conflict queue for contested belief revisions.

Facts themselves live in signal_objects (object_type='fact') with
valid_from/valid_to temporal validity; no new fact table is needed.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_facts_v1"


def apply_wiki_facts_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fact_conflicts (
            conflict_id TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            incumbent_object_id TEXT NOT NULL,
            challenger_value TEXT NOT NULL,
            challenger_confidence REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fact_conflicts_subject
            ON fact_conflicts(subject_entity_id, predicate);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_facts_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS fact_conflicts")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
