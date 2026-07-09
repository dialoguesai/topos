"""P2.1 (PLAN_PROVENANCE_SPLIT): activity_events gains hostname + content.

The migration adds two nullable columns to activity_events, is idempotent
(re-runs cheaply after the legacy CREATE TABLE IF NOT EXISTS DDL), and is
registered in BOTH apply_all_migrations and ensure_migrations_applied.
"""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations import (
    apply_all_migrations,
    ensure_migrations_applied,
)
from topos.storage.db.migrations.activity_events_content_v1 import (
    MIGRATION_ID,
    apply_activity_events_content_v1_up,
)


def _cols(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_columns_added_to_activity_events() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    cols = _cols(conn, "activity_events")
    assert "hostname" in cols
    assert "content" in cols


def test_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    before = _cols(conn, "activity_events")
    # Re-running (both the full runner and the single migration) must not raise
    # or duplicate columns.
    apply_all_migrations(conn)
    ensure_migrations_applied(conn)
    apply_activity_events_content_v1_up(conn)
    after = _cols(conn, "activity_events")
    assert after == before
    # Ledger row recorded exactly once.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()[0]
        == 1
    )


def test_columns_added_after_migration_row_and_late_table() -> None:
    # Guard the ensure-path ordering: a DB that recorded the migration id early
    # (or created activity_events via legacy DDL after) must still get the
    # columns — the adds are not ledger-gated.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE wiki_schema_migrations (
               migration_id TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,)
    )
    conn.execute(
        """CREATE TABLE activity_events (
               event_id TEXT PRIMARY KEY, activity_type TEXT, url TEXT, title TEXT,
               occurred_at TEXT, source_id TEXT, metadata_json TEXT)"""
    )
    conn.commit()

    apply_activity_events_content_v1_up(conn)

    cols = _cols(conn, "activity_events")
    assert "hostname" in cols
    assert "content" in cols


def test_no_crash_when_table_absent() -> None:
    conn = sqlite3.connect(":memory:")
    # No activity_events table — the guard must skip silently.
    apply_activity_events_content_v1_up(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()[0]
        == 1
    )
