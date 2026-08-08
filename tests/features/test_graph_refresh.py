"""Debounced post-enrichment graph refresh (fresh-install fix, 1.2.x).

rebuild_entity_graph had exactly one caller — a manual HTTP endpoint — so a
fresh node ingested data but never materialized facts/goals/places, never
stamped provenance roles, never computed neighborhoods: every 1.2.0 headline
feature shipped dark. The refresher turns enrichment completion into a
debounced rebuild.
"""

from __future__ import annotations

import threading
import time

import pytest

from topos.features.entities import graph_refresh


@pytest.fixture(autouse=True)
def _reset_refresher():
    graph_refresh.reset_for_tests()
    yield
    graph_refresh.reset_for_tests()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_marks_coalesce_into_one_rebuild(monkeypatch):
    calls = []
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.2")
    graph_refresh.reset_for_tests(rebuild_fn=lambda: calls.append(time.time()))

    for _ in range(5):
        graph_refresh.mark_graph_dirty()
        time.sleep(0.02)

    assert _wait_for(lambda: len(calls) == 1)
    time.sleep(0.4)
    assert len(calls) == 1, "burst of marks must coalesce into a single rebuild"


def test_mark_during_rebuild_schedules_followup(monkeypatch):
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.1")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_rebuild():
        calls.append(time.time())
        started.set()
        release.wait(timeout=5)

    graph_refresh.reset_for_tests(rebuild_fn=slow_rebuild)
    graph_refresh.mark_graph_dirty()
    assert started.wait(timeout=5)
    # dirty again while the rebuild is in flight
    graph_refresh.mark_graph_dirty()
    release.set()
    assert _wait_for(lambda: len(calls) == 2), "in-flight dirt must trigger a follow-up rebuild"


def test_rebuild_errors_do_not_kill_the_refresher(monkeypatch):
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.1")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    graph_refresh.reset_for_tests(rebuild_fn=flaky)
    graph_refresh.mark_graph_dirty()
    assert _wait_for(lambda: len(calls) == 1)
    graph_refresh.mark_graph_dirty()
    assert _wait_for(lambda: len(calls) == 2), "a failed rebuild must not disable future refreshes"


def test_disabled_via_env(monkeypatch):
    calls = []
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH", "off")
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.05")
    graph_refresh.reset_for_tests(rebuild_fn=lambda: calls.append(1))
    graph_refresh.mark_graph_dirty()
    time.sleep(0.3)
    assert calls == []


def test_status_reports_dirty_and_last_run(monkeypatch):
    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.1")
    graph_refresh.reset_for_tests(rebuild_fn=lambda: None)
    assert graph_refresh.status()["last_run_at"] is None
    graph_refresh.mark_graph_dirty()
    assert _wait_for(lambda: graph_refresh.status()["last_run_at"] is not None)
    assert graph_refresh.status()["dirty"] is False


def test_graph_rebuilds_on_startup_after_debounce_interrupt(tmp_path, monkeypatch):
    import sqlite3

    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    db = sqlite3.connect(str(tmp_path / "graph.db"))
    apply_pipeline_jobs_v1_up(db)
    db.execute("UPDATE graph_materialization_state SET dirty_generation=2, materialized_generation=0 WHERE id=1")
    db.commit()

    calls = []

    def _rebuild():
        calls.append(1)
        db.execute(
            "UPDATE graph_materialization_state SET materialized_generation=dirty_generation WHERE id=1"
        )
        db.commit()

    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "60")
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    graph_refresh.reset_for_tests(rebuild_fn=_rebuild)
    graph_refresh.reconcile_graph_on_startup(db)
    assert _wait_for(lambda: len(calls) == 1)
    row = db.execute(
        "SELECT dirty_generation, materialized_generation FROM graph_materialization_state WHERE id=1"
    ).fetchone()
    assert row[0] == row[1]
    db.close()



def test_default_rebuild_does_not_wrap_the_rebuild_in_the_gate():
    """The rebuild gates its own write phases (M2.2). The gate is a reentrant
    RLock, so an outer with_db_write() here would silently reinstate the
    whole-rebuild exclusive hold (~120s observed 2026-08-07) with no test
    failing anywhere else."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(graph_refresh._default_rebuild))
    tree = ast.parse(src)

    class _Finder(ast.NodeVisitor):
        wrapped = False

        def visit_With(self, node: ast.With) -> None:
            gate_names = set()
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    fn = expr.func
                    gate_names.add(getattr(fn, "id", None) or getattr(fn, "attr", None))
            if "with_db_write" in gate_names and "rebuild_entity_graph" in ast.dump(
                ast.Module(body=node.body, type_ignores=[])
            ):
                self.wrapped = True
            self.generic_visit(node)

    finder = _Finder()
    finder.visit(tree)
    assert not finder.wrapped, (
        "_default_rebuild wraps rebuild_entity_graph in with_db_write — the "
        "reentrant gate makes every internal phase hold a no-op and the whole "
        "rebuild becomes one exclusive section again"
    )
