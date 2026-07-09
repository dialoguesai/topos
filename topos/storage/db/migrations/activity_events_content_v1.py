"""P2.1: activity_events gains ``hostname`` + ``content`` columns.

PLAN_PROVENANCE_SPLIT P2.1 — the browser plugin already captures a page's
hostname and, for highlight/selection events, the selected span; the browser
activity mapper dropped both and activity_events had no column to hold them, so
a highlighted span (Annotate-grade engagement, the one expression-grade browser
signal) never reached canonical storage or the index. This migration adds the
two columns; the mapper (canonicalization/mappers/browser_activity_mapper.py)
populates them for highlight rows and mirrors the span into ``metadata_json``
(``highlight``) so the query layer can surface it without a column-select
change in the canonical adapter.

Idempotent: the column adds are PRAGMA-guarded and re-run cheaply after the
legacy ``CREATE TABLE IF NOT EXISTS activity_events`` DDL (same pattern as the
disclosure/period/actor_role column migrations, which is why this migration is
registered in BOTH apply_all_migrations and ensure_migrations_applied and does
not gate its column adds on the migration ledger). No backfill: pre-existing
rows keep NULL, which every reader treats as the pre-P2.1 world.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "activity_events_content_v1"

_ACTIVITY_COLUMNS = (
    ("hostname", "TEXT"),
    ("content", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def apply_activity_events_content_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for column, col_type in _ACTIVITY_COLUMNS:
        _add_column_if_missing(conn, "activity_events", column, col_type)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
