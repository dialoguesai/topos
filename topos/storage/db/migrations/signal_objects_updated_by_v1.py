"""Add updated_by column to signal_objects for upsert compatibility."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "signal_objects_updated_by_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def apply_signal_objects_updated_by_v1_up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "signal_objects"):
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_objects)").fetchall()}
    if "updated_by" not in cols:
        conn.execute(
            "ALTER TABLE signal_objects ADD COLUMN updated_by TEXT NOT NULL DEFAULT 'system'"
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
