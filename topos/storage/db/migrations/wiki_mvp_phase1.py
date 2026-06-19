"""Wiki MVP Phase 1: canonical provenance, activity_events, ingest audit."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_phase1"

PROVENANCE_COLUMNS = (
    ("source_record_id", "TEXT"),
    ("ingested_at", "TEXT"),
    ("sync_batch_id", "TEXT"),
    ("content_hash", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def up(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase1_up(conn)


def down(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase1_down(conn)


def apply_wiki_mvp_phase1_up(conn: sqlite3.Connection) -> None:
    for table in (
        "ai_chat_messages",
        "ai_chat_conversations",
        "conversation_messages",
        "conversations",
        "activity_events",
    ):
        for column, col_type in PROVENANCE_COLUMNS:
            if column != "content_hash" or table in ("ai_chat_messages", "conversation_messages"):
                _add_column_if_missing(conn, table, column, col_type)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS canonical_source_mappings (
            source_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            canonical_table TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (source_id, source_record_id)
        );

        CREATE TABLE IF NOT EXISTS activity_events (
            event_id TEXT PRIMARY KEY,
            activity_type TEXT,
            url TEXT,
            title TEXT,
            occurred_at TEXT,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_activity_events_source ON activity_events(source_id, occurred_at);

        CREATE TABLE IF NOT EXISTS calendar_events (
            event_id TEXT PRIMARY KEY,
            title TEXT,
            starts_at TEXT,
            ends_at TEXT,
            source_id TEXT,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            contact_id TEXT NOT NULL PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            display_name TEXT,
            known_usernames_json TEXT,
            is_self INTEGER NOT NULL DEFAULT 0,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS contact_identifiers (
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            identifier TEXT NOT NULL,
            identifier_type TEXT,
            contact_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (dataset_id, source_id, identifier)
        );

        CREATE TABLE IF NOT EXISTS ingest_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            sync_batch_id TEXT NOT NULL,
            source_id TEXT,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            records_in INTEGER DEFAULT 0,
            records_out INTEGER DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ingest_audit_batch ON ingest_audit(sync_batch_id);
        """
    )

    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()


def apply_wiki_mvp_phase1_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS ingest_audit;
        DROP TABLE IF EXISTS calendar_events;
        DROP TABLE IF EXISTS activity_events;
        DROP TABLE IF EXISTS canonical_source_mappings;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
