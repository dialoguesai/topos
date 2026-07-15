"""Documents canonical lane (PLAN_CANONICAL_CALENDAR_DOCUMENTS Part A).

Notion pages and Google Drive files were being parked on the `conversations`
group as a stopgap — a saved page ingested as if it were a chat message. This
adds a first-class `documents` canonical table so Notion/Drive route to a
real document-shaped row: doc_id, title, content, url, mime_type, author,
created_at, modified_at, plus the standard provenance quartet
(source_id/source_record_id/ingested_at/sync_batch_id/metadata_json) every
canonical table carries.

A brand-new table appears for free on every node via
``CREATE TABLE IF NOT EXISTS`` — no ALTER, no backfill required. The ledger
gate at the call site (migrations/__init__.py) is a cheap optimization, not a
correctness requirement, since CREATE TABLE/INDEX IF NOT EXISTS are already
idempotent.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "documents_v1"


def apply_documents_v1_up(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS documents (
            doc_id           TEXT PRIMARY KEY,
            title            TEXT,
            content          TEXT,
            url              TEXT,
            mime_type        TEXT,
            author           TEXT,
            created_at       TEXT,
            modified_at      TEXT,
            source_id        TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at      TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id    TEXT,
            metadata_json    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id, modified_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
