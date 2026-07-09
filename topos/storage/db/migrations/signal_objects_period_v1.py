"""B2.1: promote period_start/period_end from payload JSON to indexed columns.

signal_objects payloads (facts, stat-derived artifacts) carry real-world event
periods as payload keys (``period_start``/``period_end``); event-time queries
had to json_extract every row. This promotes them to real columns with partial
indexes and backfills from existing payloads. Writers (FactStore,
SignalObjectStore) stamp the columns on insert going forward.

Idempotent: column adds are PRAGMA-guarded and re-run on every boot (cheap);
the backfill table scan runs once, guarded by the migration ledger.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "signal_objects_period_v1"

_PERIOD_COLUMNS = ("period_start", "period_end")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _migration_applied(conn: sqlite3.Connection, migration_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def apply_signal_objects_period_v1_up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "signal_objects"):
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_objects)").fetchall()}
    for col in _PERIOD_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE signal_objects ADD COLUMN {col} TEXT")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_signal_objects_period_start
           ON signal_objects(period_start) WHERE period_start IS NOT NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_signal_objects_period_end
           ON signal_objects(period_end) WHERE period_end IS NOT NULL"""
    )

    if not _migration_applied(conn, MIGRATION_ID):
        # One-time backfill from existing payloads. json_extract returns NULL
        # for absent keys, so only rows that actually carry a period change.
        for col in _PERIOD_COLUMNS:
            conn.execute(
                f"""
                UPDATE signal_objects
                SET {col} = json_extract(payload_json, '$.{col}')
                WHERE {col} IS NULL
                  AND json_extract(payload_json, '$.{col}') IS NOT NULL
                """
            )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
