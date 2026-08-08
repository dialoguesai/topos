"""SQLite DDL for routines (functional feature storage on local Topos nodes)."""

from __future__ import annotations

import logging
import sqlite3

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.routines.schema")


def ensure_routines_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # DDL takes SQLite's write lock at execute time — gate it with the
        # commit (write_gate lock-order inversion); store calls this per request.
        with with_db_write():
            _ensure_routines_tables(conn)
            commit_connection(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_routines_schema failed: %s", exc)
        raise


def _ensure_routines_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            engine_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            next_run_at TEXT,
            deleted_at TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_routines_owner_engine_active
        ON routines (owner_user_id, engine_id, updated_at DESC)
        WHERE deleted_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_routines_due_scheduled
        ON routines (next_run_at)
        WHERE deleted_at IS NULL AND enabled = 1
          AND trigger_type IN ('weekly', 'scheduled')
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routine_runs (
            id TEXT PRIMARY KEY,
            routine_id TEXT NOT NULL,
            owner_user_id TEXT NOT NULL,
            engine_id TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_runs_idempotency
        ON routine_runs (idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_routine_runs_routine_status
        ON routine_runs (routine_id, status)
        """
    )
