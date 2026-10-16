"""Per-file byte-offset cursors for the AI coding-tool transcript readers (CONTRACTS §2.6).

``ingestion_checkpoints`` keeps one ``last_record_id`` per (dataset, schema).
A transcript source is thousands of append-only files, so each file carries
its own cursor here, keyed by ``sha256(absolute path)``: the path itself names
the home folder and the project, and never reaches a table.

Lands at a release cut like 63, 69, 75 and 76: registering it stamps the
schema version past any engine that predates it. One small table, no row read
or changed. ``ingestion/checkpoints/file_cursors.py`` carries the same DDL for
connections the runner has not migrated (tests, L6's fakes).
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "ingestion_file_cursors_v1"


def apply_ingestion_file_cursors_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
CREATE TABLE IF NOT EXISTS ingestion_file_cursors (
    source_id TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    offset INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, path_hash)
)
"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_ingestion_file_cursors_v1_down(conn: sqlite3.Connection) -> None:
    # The table is left in place: an older engine ignores it, and a re-upgrade
    # resumes every file where it stopped. Only the ledger stamp reverses.
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
