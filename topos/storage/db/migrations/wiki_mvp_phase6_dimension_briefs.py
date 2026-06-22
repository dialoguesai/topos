"""Wiki MVP Phase 6: versioned signal dimension living briefs."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_phase6_dimension_briefs_v1"


def apply_wiki_mvp_phase6_dimension_briefs_up(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS signal_dimension_briefs (
            brief_id TEXT PRIMARY KEY,
            signal_dimension TEXT NOT NULL UNIQUE,
            head_revision_id TEXT NOT NULL,
            structured_json TEXT NOT NULL,
            markdown_body TEXT NOT NULL,
            revision_number INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by TEXT NOT NULL DEFAULT 'system'
        );
        CREATE INDEX IF NOT EXISTS idx_signal_dimension_briefs_dimension
            ON signal_dimension_briefs(signal_dimension);

        CREATE TABLE IF NOT EXISTS signal_dimension_brief_revisions (
            revision_id TEXT PRIMARY KEY,
            brief_id TEXT NOT NULL,
            parent_revision_id TEXT,
            revision_number INTEGER NOT NULL,
            change_kind TEXT NOT NULL,
            structured_json TEXT NOT NULL,
            markdown_body TEXT NOT NULL,
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by TEXT NOT NULL DEFAULT 'system',
            FOREIGN KEY (brief_id) REFERENCES signal_dimension_briefs(brief_id)
        );
        CREATE INDEX IF NOT EXISTS idx_signal_brief_revisions_brief
            ON signal_dimension_brief_revisions(brief_id, revision_number);
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_mvp_phase6_dimension_briefs_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS signal_dimension_brief_revisions;
        DROP TABLE IF EXISTS signal_dimension_briefs;
        DELETE FROM wiki_schema_migrations WHERE migration_id=?;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,))
    conn.commit()
