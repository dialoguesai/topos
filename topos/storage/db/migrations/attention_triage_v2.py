"""Attention triage v2: calibration + labeling columns (WS1/WS2,
PLAN_ATTENTION_ANALYTICS_EXECUTION.md).

Adds to triage_verdicts: length-residualized compression novelty, the junk
flag (BT9 blob filter), and user ground-truth labels (verdict-review loop).
Idempotent PRAGMA-guarded column adds; safe to run unconditionally.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "attention_triage_v2"

_COLUMNS = (
    ("comp_resid", "REAL"),
    ("junk", "INTEGER DEFAULT 0"),
    ("user_label", "TEXT"),
    ("user_labeled_at", "TEXT"),
)


def _add_column_if_missing(conn, table, column, col_type) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def apply_attention_triage_v2_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for column, col_type in _COLUMNS:
        _add_column_if_missing(conn, "triage_verdicts", column, col_type)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
