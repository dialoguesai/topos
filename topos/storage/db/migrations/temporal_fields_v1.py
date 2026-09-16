"""Separated temporal records beside the legacy time columns (step 5).

Adds two nullable TEXT columns and nothing else:

- ``signal_objects.temporal_json``: ``topos-fact-temporal/v1``, written once
  when a fact row is inserted;
- ``conversation_messages.event_time_json``: ``topos-event-time/v1``, written
  once when a message row is inserted.

No default, no CHECK and no backfill, so the ALTER is a schema-only change with
no table scan, and every existing row stays NULL. That matters beyond cost: the
permission evidence review hashes every non-NULL column of a leaf and a fact,
so a backfilled value would stale every review on the node. A NULL column is not
on that surface.

``always_run``: the column adds are PRAGMA-guarded and cheap, and legacy DDL
paths that recreate these tables would otherwise leave them missing.

Release note. Registering this stamps the database's schema version, and an
engine that knows fewer migrations refuses a newer stamp. Like specs 63 and 69,
it must land at a release cut; do not run an engine built from a branch carrying
it against a node whose installed engine predates it.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "temporal_fields_v1"

COLUMNS: tuple[tuple[str, str], ...] = (
    ("signal_objects", "temporal_json"),
    ("conversation_messages", "event_time_json"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def apply_temporal_fields_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for table, column in COLUMNS:
        if not _table_exists(conn, table):
            continue
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether a writer may include the column. Checked by writers, never assumed."""
    try:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except sqlite3.Error:
        return False
