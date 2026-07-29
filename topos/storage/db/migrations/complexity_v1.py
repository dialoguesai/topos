"""Complexity snapshot storage (PLAN_COMPLEXITY_DATA_PAGE.md M0).

One row per cached metric set from the complexity engine: `summary`
(id `summary_latest`), per-week `shift_week`, `shift_timeline`, `influence`,
and append-only `focus_baseline` (one per day). Snapshots are derived caches,
recomputable at any time — never ingest-time facts. The engine's store keeps a
harmless on-demand CREATE IF NOT EXISTS for pre-migration databases.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "complexity_v1"


def apply_complexity_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS complexity_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            metric_set TEXT NOT NULL,
            week_start TEXT,
            metrics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_complexity_snapshots_metric_week
        ON complexity_snapshots(metric_set, week_start)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
