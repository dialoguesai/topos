"""Engine-wide scan of active routine runs — the stale sweep's input.

The scheduler only ever asks for routines that are *due*. A run stranded under a
manual-only or disabled routine is invisible to that query, so it needs a scan
that ignores the schedule entirely.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.routines.schema import ensure_routines_schema
from topos.routines.store import list_active_runs, list_due_scheduled_routines

ENGINE = "engine-1"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_routines_schema(connection)
    return connection


def _add_routine(conn, routine_id: str, *, trigger_type: str, enabled: int = 1, engine: str = ENGINE) -> None:
    conn.execute(
        """
        INSERT INTO routines (id, owner_user_id, engine_id, enabled, trigger_type, next_run_at, payload_json, updated_at)
        VALUES (?, 'user-1', ?, ?, ?, NULL, ?, '2026-07-29T10:00:00+00:00')
        """,
        (routine_id, engine, enabled, trigger_type, json.dumps({"id": routine_id})),
    )


def _add_run(conn, run_id: str, routine_id: str, *, status: str, updated_at: str, engine: str = ENGINE) -> None:
    conn.execute(
        """
        INSERT INTO routine_runs (id, routine_id, owner_user_id, engine_id, status, payload_json, updated_at)
        VALUES (?, ?, 'user-1', ?, ?, ?, ?)
        """,
        (run_id, routine_id, engine, status, json.dumps({"id": run_id}), updated_at),
    )


def test_surfaces_runs_under_routines_that_never_come_up_due(conn):
    _add_routine(conn, "manual-only", trigger_type="manual")
    _add_routine(conn, "disabled-weekly", trigger_type="weekly", enabled=0)
    _add_run(conn, "run-1", "manual-only", status="running", updated_at="2026-07-29T10:00:00+00:00")
    _add_run(conn, "run-2", "disabled-weekly", status="queued", updated_at="2026-07-29T11:00:00+00:00")

    # Neither routine is reachable through the scheduler's due query...
    assert list_due_scheduled_routines(conn, engine_id=ENGINE) == []
    # ...but both corpses are reachable through this one.
    assert sorted(r["id"] for r in list_active_runs(conn, engine_id=ENGINE)) == ["run-1", "run-2"]


def test_ignores_finished_runs(conn):
    _add_routine(conn, "r1", trigger_type="manual")
    _add_run(conn, "done", "r1", status="completed", updated_at="2026-07-29T10:00:00+00:00")
    _add_run(conn, "dead", "r1", status="failed", updated_at="2026-07-29T10:00:00+00:00")
    _add_run(conn, "live", "r1", status="running", updated_at="2026-07-29T10:00:00+00:00")

    assert [r["id"] for r in list_active_runs(conn, engine_id=ENGINE)] == ["live"]


def test_scopes_to_the_engine_asked_for(conn):
    _add_routine(conn, "mine", trigger_type="manual")
    _add_routine(conn, "theirs", trigger_type="manual", engine="engine-2")
    _add_run(conn, "run-mine", "mine", status="running", updated_at="2026-07-29T10:00:00+00:00")
    _add_run(conn, "run-theirs", "theirs", status="running", updated_at="2026-07-29T10:00:00+00:00", engine="engine-2")

    assert [r["id"] for r in list_active_runs(conn, engine_id=ENGINE)] == ["run-mine"]


def test_returns_oldest_first_within_the_limit(conn):
    """A capped sweep has to spend its budget on the worst corpses."""
    _add_routine(conn, "r1", trigger_type="manual")
    _add_run(conn, "newest", "r1", status="running", updated_at="2026-07-29T12:00:00+00:00")
    _add_run(conn, "oldest", "r1", status="running", updated_at="2026-07-20T09:00:00+00:00")
    _add_run(conn, "middle", "r1", status="running", updated_at="2026-07-29T09:00:00+00:00")

    assert [r["id"] for r in list_active_runs(conn, engine_id=ENGINE)] == ["oldest", "middle", "newest"]
    assert [r["id"] for r in list_active_runs(conn, engine_id=ENGINE, limit=2)] == ["oldest", "middle"]
