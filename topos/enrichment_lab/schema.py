"""SQLite DDL for Enrichment Lab tables (engine-owned)."""

from __future__ import annotations

import logging
import sqlite3

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.enrichment_lab.schema")


def ensure_enrichment_lab_schema(conn: sqlite3.Connection) -> None:
    """Create Enrichment Lab tables and indices if missing."""
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # DDL takes SQLite's write lock at execute time — gate it with the
        # commit (write_gate lock-order inversion); store calls this per request.
        with with_db_write():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_lab_job_group (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    dataset_kind TEXT NOT NULL,
                    bundle_id TEXT,
                    bundle_version TEXT,
                    source_id TEXT,
                    sample_limit INTEGER,
                    status TEXT NOT NULL,
                    models_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '{}',
                    preferred_model_tag TEXT,
                    group_notes TEXT,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_lab_run (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    model_tag TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    latency_ms INTEGER,
                    error_code TEXT,
                    input_json TEXT,
                    output_json TEXT,
                    metrics_json TEXT,
                    user_quality_score_0_10 INTEGER,
                    user_liked INTEGER,
                    user_note TEXT,
                    rated_at TEXT,
                    FOREIGN KEY (group_id) REFERENCES enrichment_lab_job_group(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enrichment_lab_group_job_created "
                "ON enrichment_lab_job_group (job_id, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enrichment_lab_run_group ON enrichment_lab_run (group_id)"
            )
            commit_connection(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_enrichment_lab_schema failed: %s", exc)
        raise
