"""SQLite DDL for Filter Lab tables (engine-owned)."""

from __future__ import annotations

import logging
import sqlite3

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.filter_lab.schema")


def ensure_filter_lab_schema(conn: sqlite3.Connection) -> None:
    """Create Filter Lab tables and indices if missing."""
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # DDL takes SQLite's write lock at execute time — gate it with the
        # commit (write_gate lock-order inversion); store calls this per request.
        with with_db_write():
            _ensure_filter_lab_tables(conn)
            commit_connection(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_filter_lab_schema failed: %s", exc)
        raise


def _ensure_filter_lab_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_lab_job_group (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            filter_id TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            bundle_version TEXT NOT NULL,
            status TEXT NOT NULL,
            baseline_models_json TEXT NOT NULL DEFAULT '[]',
            pulled_models_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            preferred_model_tag TEXT,
            group_notes TEXT,
            options_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_lab_run (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            model_tag TEXT NOT NULL,
            record_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            latency_ms INTEGER,
            error_code TEXT,
            input_hash TEXT,
            input_text TEXT,
            output_text TEXT,
            metrics_json TEXT,
            user_quality_score_0_10 INTEGER,
            user_liked INTEGER,
            user_note TEXT,
            rated_at TEXT,
            FOREIGN KEY (group_id) REFERENCES filter_lab_job_group(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_lab_model_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            model_tag TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES filter_lab_job_group(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_filter_lab_job_group_filter_created "
        "ON filter_lab_job_group (filter_id, created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filter_lab_run_group ON filter_lab_run (group_id)")
    cur = conn.execute("PRAGMA table_info(filter_lab_job_group)")
    cols = [r[1] for r in cur.fetchall()]
    if cols and "options_json" not in cols:
        conn.execute(
            "ALTER TABLE filter_lab_job_group ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'"
        )
        logger.info(
            "filter_lab schema migrated: filter_lab_job_group.options_json added"
        )
