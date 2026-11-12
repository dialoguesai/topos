"""At most one automatic graph rebuild per TOPOS_GRAPH_REFRESH_MIN_INTERVAL_S (default 2 h), across restarts.

Every message changes the graph's input fingerprint, so on the owner's node (4 Nov 2026) the rebuild ran 26% of the
time, 603-1113 s each, and moved 0-7 of 68,794 edges. What these stop: a changed input rebuilding inside the
interval (through the debounce, the pipeline's inline fill or the startup reconcile), a restart that forgets when the
last rebuild ended, a held graph that is never looked at again, and a clock that went back holding the graph for hours.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from topos.features.entities import graph_inputs, graph_refresh
from topos.storage.db.migrations import apply_all_migrations

HOUR = 3600.0


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def node(conn, monkeypatch):
    """The real refresher on this database, every message changing the inputs, the rebuild counted."""
    fresh = count()
    monkeypatch.setattr(graph_inputs, "graph_input_fingerprint", lambda c: f"inputs-{next(fresh)}")
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.core.state.close_thread_db_connection", lambda: None)
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "3600")
    monkeypatch.delenv("TOPOS_GRAPH_REFRESH_MIN_INTERVAL_S", raising=False)
    calls = []

    def rebuild(c):
        calls.append(1)
        return {}

    def restart():
        graph_refresh.reset_for_tests(rebuild_fn=lambda: graph_refresh.rebuild_if_inputs_changed(conn, rebuild=rebuild))

    restart()
    yield calls, restart
    graph_refresh.reset_for_tests()


def _mark(conn):
    conn.execute("UPDATE graph_materialization_state SET dirty_generation = dirty_generation + 1 WHERE id=1")
    conn.commit()


def _ended(conn, ago_s: float):
    at = (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()
    conn.execute("UPDATE graph_materialization_state SET last_run_at=?", (at,))
    conn.commit()


def _pending(name="topos-graph-refresh-due"):
    timer = graph_refresh._refresher._timer
    return timer is not None and timer.name == name


def _fire_now():
    """The pending timer firing now (cancelled first, so no second thread waits out the interval)."""
    if graph_refresh._refresher._timer is not None:
        graph_refresh._refresher._timer.cancel()
    graph_refresh._refresher._fire()


def _generations(conn):
    return conn.execute("SELECT dirty_generation, materialized_generation FROM graph_materialization_state").fetchone()


def test_changed_inputs_inside_the_interval_do_not_rebuild_and_one_look_is_armed_for_its_end(conn, node):
    calls, _ = node
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}  # no rebuild yet: the first one is not held
    _mark(conn)
    out = graph_refresh.rebuild_if_inputs_changed(conn, rebuild=lambda c: calls.append(1) or {})
    assert out["skipped"] == "interval" and 2 * HOUR - 60 < out["due_in_s"] <= 2 * HOUR
    assert calls == [1] and _pending()
    dirty, materialized = _generations(conn)
    assert dirty > materialized, "a held graph stays dirty, so the look at the end of the interval rebuilds"


def test_every_automatic_path_is_held(conn, node):
    calls, _ = node
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"skipped": "interval"}  # the pipeline's inline fill
    _fire_now()  # the debounce firing
    graph_refresh.reconcile_graph_on_startup(conn)
    assert calls == [1] and _pending()


def test_a_mark_inside_the_interval_replaces_the_due_look_and_that_look_arms_it_again(conn, node):
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    assert _pending()
    graph_refresh.mark_graph_dirty()
    assert _pending("topos-graph-refresh-debounce")
    _fire_now()
    assert _pending()


def test_a_restart_inside_the_interval_does_not_rebuild(conn, node):
    calls, restart = node
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    _ended(conn, 25 * 60)
    _mark(conn)
    restart()
    graph_refresh.reconcile_graph_on_startup(conn)
    assert calls == [1] and _pending()
    assert 95 * 60 - 60 < graph_refresh._interval_left(
        conn.execute("SELECT last_run_at FROM graph_materialization_state").fetchone()[0], 2 * HOUR) <= 95 * 60


def test_once_the_interval_has_passed_a_change_rebuilds(conn, node):
    calls, _ = node
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    _ended(conn, 2 * HOUR + 5)
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True} and calls == [1, 1]


@pytest.mark.parametrize("last_run_at", ["2999-01-01T00:00:00+00:00", "not a time"])
def test_a_time_from_the_future_or_unreadable_does_not_hold(conn, node, last_run_at):
    calls, _ = node
    conn.execute("UPDATE graph_materialization_state SET last_run_at=?", (last_run_at,))
    conn.commit()
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}


def test_the_interval_is_a_setting_and_0_turns_it_off(conn, node, monkeypatch):
    calls, _ = node
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_MIN_INTERVAL_S", "600")
    _mark(conn)
    graph_refresh.refresh_now_if_dirty()
    _ended(conn, 11 * 60)
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True}
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_MIN_INTERVAL_S", "0")
    _mark(conn)
    assert graph_refresh.refresh_now_if_dirty() == {"ran": True} and calls == [1, 1, 1]


def test_the_owners_rebuild_is_not_held():
    from topos.api import signal

    route = inspect.getsource(signal)
    start = route.index('@router.post("/entities/graph/rebuild")')
    body = route[start: route.index("@router.", start + 1)]
    assert "run_graph_rebuild" in body and "rebuild_if_inputs_changed" not in body and "refresh_now" not in body
