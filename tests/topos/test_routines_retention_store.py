"""Engine-side inputs for routine-run retention.

The control plane decides *what* to keep (it is the side that knows about
since_last_successful_run); the node only has to answer "which finished runs
are older than this" and "delete these ids".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.routines.schema import ensure_routines_schema
from topos.routines.store import delete_runs, list_expired_runs

ENGINE = "engine-1"
CUTOFF = "2026-05-24T12:00:00+00:00"  # 90 days before 2026-08-22


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_routines_schema(connection)
    connection.execute(
        """
        INSERT INTO routines (id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, payload_json, updated_at)
        VALUES ('r1', 'user-1', ?, 1, 'manual', NULL, '{}', '2026-08-22T12:00:00+00:00')
        """,
        (ENGINE,),
    )
    return connection


def _add_run(conn, run_id: str, *, status: str, updated_at: str, engine: str = ENGINE) -> None:
    conn.execute(
        """
        INSERT INTO routine_runs (id, routine_id, owner_user_id, engine_id, status, payload_json, updated_at)
        VALUES (?, 'r1', 'user-1', ?, ?, ?, ?)
        """,
        (run_id, engine, status, json.dumps({"id": run_id}), updated_at),
    )


def test_lists_finished_runs_older_than_the_cutoff_oldest_first(conn):
    _add_run(conn, "ancient", status="completed", updated_at="2026-01-02T00:00:00+00:00")
    _add_run(conn, "old", status="failed", updated_at="2026-04-01T00:00:00+00:00")
    _add_run(conn, "recent", status="completed", updated_at="2026-08-01T00:00:00+00:00")

    expired = list_expired_runs(conn, engine_id=ENGINE, cutoff=CUTOFF)
    assert [r["id"] for r in expired] == ["ancient", "old"]


@pytest.mark.parametrize("status", ["queued", "waiting_for_engine", "running"])
def test_never_offers_an_active_run_however_old(conn, status: str):
    """Closing a stuck run is the stale sweep's job; retention must not race it."""
    _add_run(conn, "stuck", status=status, updated_at="2026-01-02T00:00:00+00:00")
    assert list_expired_runs(conn, engine_id=ENGINE, cutoff=CUTOFF) == []


def test_scopes_to_the_engine_asked_for(conn):
    conn.execute(
        """
        INSERT INTO routines (id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, payload_json, updated_at)
        VALUES ('r2', 'user-1', 'engine-2', 1, 'manual', NULL, '{}', '2026-08-22T12:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO routine_runs (id, routine_id, owner_user_id, engine_id, status, payload_json, updated_at)
        VALUES ('theirs', 'r2', 'user-1', 'engine-2', 'completed', '{}', '2026-01-02T00:00:00+00:00')
        """
    )
    _add_run(conn, "mine", status="completed", updated_at="2026-01-02T00:00:00+00:00")

    assert [r["id"] for r in list_expired_runs(conn, engine_id=ENGINE, cutoff=CUTOFF)] == ["mine"]


def test_honours_the_limit(conn):
    for i in range(5):
        _add_run(conn, f"run-{i}", status="completed", updated_at=f"2026-01-0{i + 1}T00:00:00+00:00")
    assert len(list_expired_runs(conn, engine_id=ENGINE, cutoff=CUTOFF, limit=3)) == 3


def test_delete_removes_the_rows_and_counts_them(conn):
    _add_run(conn, "gone-1", status="completed", updated_at="2026-01-02T00:00:00+00:00")
    _add_run(conn, "gone-2", status="failed", updated_at="2026-01-03T00:00:00+00:00")
    _add_run(conn, "kept", status="completed", updated_at="2026-08-01T00:00:00+00:00")

    assert delete_runs(conn, ["gone-1", "gone-2"]) == 2
    remaining = conn.execute("SELECT id FROM routine_runs").fetchall()
    assert [row["id"] for row in remaining] == ["kept"]


def test_delete_counts_only_rows_that_existed(conn):
    _add_run(conn, "real", status="completed", updated_at="2026-01-02T00:00:00+00:00")
    assert delete_runs(conn, ["real", "never-existed"]) == 1


def test_delete_of_nothing_is_a_no_op(conn):
    _add_run(conn, "safe", status="completed", updated_at="2026-01-02T00:00:00+00:00")
    assert delete_runs(conn, []) == 0
    assert conn.execute("SELECT COUNT(*) FROM routine_runs").fetchone()[0] == 1
