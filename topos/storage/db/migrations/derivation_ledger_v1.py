"""Derivation ledger: per-step execution state for upgrade re-derivations.

Schema migrations own table SHAPE; the upgrade manifests (topos/upgrades)
declare which DERIVED layers a release invalidates. This ledger is the
runner's memory: one row per (version, step), so an interrupted upgrade
resumes instead of silently leaving derived layers half-rebuilt (observed
live 2026-07-10: a restart mid re-extraction left 5% of mentions with
nothing reporting the gap).
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "derivation_ledger_v1"


def apply_derivation_ledger_v1_up(conn: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS derivation_ledger (
            version TEXT NOT NULL,
            step_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
            started_at TEXT,
            finished_at TEXT,
            detail_json TEXT,
            PRIMARY KEY (version, step_id)
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
