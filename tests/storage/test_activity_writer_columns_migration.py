"""Migration 80, activity_writer_columns_v1 (OD-52 P1): activity_events records its writer.

Three nullable columns, added by an always-run, PRAGMA-guarded step on every runner,
re-asserted after the legacy DDL, and never backfilled: an existing row keeps NULL,
because the door that wrote it is recorded nowhere and a class written now would be
forged provenance.
"""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations import apply_all_migrations, ensure_migrations_applied
from topos.storage.db.migrations import activity_writer_columns_v1 as aw
from topos.storage.db.migrations.registry import MIGRATIONS, max_migration_order

WRITER_COLUMNS = {"writer_class", "writer_app_id", "writer_dataset_id"}


def _cols(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()}


def test_spec_80_runs_on_every_start_below_the_head():
    spec = next(spec for spec in MIGRATIONS if spec.id == aw.MIGRATION_ID)
    assert spec.order == 80 and spec.always_run is True
    # 81 (off_limits_carried_waiting_v1) is the head since the 1.5.0 carry step's mark became a schema step.
    assert max_migration_order() == 81
    assert len({spec.order for spec in MIGRATIONS}) == len(MIGRATIONS)


def test_both_runners_add_the_writer_columns_and_stamp_the_head(tmp_path):
    for name, migrate in (("all", apply_all_migrations), ("ensure", lambda c: ensure_migrations_applied(c, skip_backup=True))):
        conn = sqlite3.connect(str(tmp_path / f"{name}.db"))
        try:
            migrate(conn)
            assert WRITER_COLUMNS <= _cols(conn), name
            assert conn.execute("PRAGMA user_version").fetchone()[0] == max_migration_order(), name
        finally:
            conn.close()


def test_existing_rows_keep_null_and_nothing_else_moves():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE activity_events (
               event_id TEXT PRIMARY KEY, activity_type TEXT, url TEXT, title TEXT,
               occurred_at TEXT, source_id TEXT NOT NULL, source_record_id TEXT,
               ingested_at TEXT, sync_batch_id TEXT, metadata_json TEXT, content TEXT, hostname TEXT)"""
    )
    rows = [
        ("browser:v1", "visit", "https://example.test/a", "A", "2026-09-01T10:00:00Z", "browser_visits"),
        ("browser:v2", "visit", "https://example.test/b", "B", "2026-09-02T10:00:00Z", "browser_visits"),
        ("github:1", "push", "https://example.test/r", "R", "2026-09-03T10:00:00", "github_activity"),
    ]
    conn.executemany(
        "INSERT INTO activity_events (event_id, activity_type, url, title, occurred_at, source_id) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    before = conn.execute("SELECT * FROM activity_events ORDER BY event_id").fetchall()

    aw.apply_activity_writer_columns_v1_up(conn)

    assert WRITER_COLUMNS <= _cols(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM activity_events WHERE writer_class IS NOT NULL OR writer_app_id IS NOT NULL "
        "OR writer_dataset_id IS NOT NULL"
    ).fetchone()[0] == 0
    after = conn.execute(
        "SELECT event_id, activity_type, url, title, occurred_at, source_id, source_record_id, ingested_at, "
        "sync_batch_id, metadata_json, content, hostname FROM activity_events ORDER BY event_id"
    ).fetchall()
    assert after == before


def test_rerunning_is_a_no_op_and_the_ledger_row_is_written_once():
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    columns = _cols(conn)
    schema = conn.execute("PRAGMA schema_version").fetchone()
    aw.apply_activity_writer_columns_v1_up(conn)
    assert conn.execute("PRAGMA schema_version").fetchone() == schema  # this step changed no DDL
    apply_all_migrations(conn)
    assert _cols(conn) == columns
    assert conn.execute(
        "SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?", (aw.MIGRATION_ID,)
    ).fetchone()[0] == 1


def test_a_late_table_still_gets_the_columns():
    """The adds are not ledger-gated: a table created after the id was recorded is covered."""
    conn = sqlite3.connect(":memory:")
    aw.apply_activity_writer_columns_v1_up(conn)  # no table yet: records the id, adds nothing
    conn.execute(
        """CREATE TABLE activity_events (
               event_id TEXT PRIMARY KEY, activity_type TEXT, url TEXT, title TEXT,
               occurred_at TEXT, source_id TEXT, metadata_json TEXT)"""
    )
    aw.apply_activity_writer_columns_v1_up(conn)
    assert WRITER_COLUMNS <= _cols(conn)
