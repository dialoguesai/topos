"""Durable pipeline job queue for ingest, enrichment, and derivation work."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "pipeline_jobs_v1"


def _pipeline_jobs_already_applied(conn: sqlite3.Connection) -> bool:
    """True when this connection already has the migration ledger row.

    ``ensure_pipeline_jobs_schema`` is called on every enqueue/claim. Re-running
    the CREATE / INSERT OR IGNORE under the write gate on those calls is what
    stalled the event loop (and the tray healthcheck) on a live node.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def apply_pipeline_jobs_v1_up(conn: sqlite3.Connection) -> None:
    # Callers invoke this ahead of their own gated sections; the INSERT OR
    # IGNORE below takes SQLite's write lock every call, so gate it here with
    # its commit (write_gate lock-order inversion). Skip the gate entirely
    # when the ledger already records this migration — a read does not need
    # it, and taking it on the event-loop thread blocks /healthcheck.
    if _pipeline_jobs_already_applied(conn):
        return
    from ..write_gate import commit_connection, with_db_write

    with with_db_write():
        if _pipeline_jobs_already_applied(conn):
            return
        _apply_pipeline_jobs_v1_up_locked(conn)
        commit_connection(conn)


def _apply_pipeline_jobs_v1_up_locked(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            lease_owner TEXT,
            lease_expires_at TEXT,
            payload_json TEXT NOT NULL,
            progress_json TEXT,
            detail_json TEXT,
            sync_batch_id TEXT,
            source_id TEXT,
            write_id TEXT,
            idempotency_key TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_jobs_idempotency
            ON pipeline_jobs(idempotency_key)
            WHERE idempotency_key IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_recover
            ON pipeline_jobs(status, created_at);

        CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_write_id
            ON pipeline_jobs(write_id)
            WHERE write_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS pipeline_derivation_completion (
            write_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            sync_batch_id TEXT,
            source_id TEXT,
            completed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS graph_materialization_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dirty_generation INTEGER NOT NULL DEFAULT 0,
            materialized_generation INTEGER NOT NULL DEFAULT 0,
            last_run_at TEXT,
            last_error TEXT
        );

        INSERT OR IGNORE INTO graph_materialization_state (id, dirty_generation, materialized_generation)
            VALUES (1, 0, 0);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
