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


def test_deferred_rebuild_rearms_instead_of_failing(monkeypatch):
    """A rebuild stepping aside for a derivation batch (WriteGateDeferred) is
    not an error: it must re-arm the debounce and run once the coast is clear."""
    from topos.storage.db.write_gate import WriteGateDeferred

    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "0.05")
    calls = []

    def rebuild():
        calls.append(1)
        if len(calls) == 1:
            raise WriteGateDeferred("derivation in flight")

    graph_refresh.reset_for_tests(rebuild_fn=rebuild)
    graph_refresh.mark_graph_dirty()
    assert _wait_for(lambda: len(calls) >= 2), "deferred rebuild must retry"
    assert graph_refresh.status()["last_error"] is None


def test_default_rebuild_defers_while_derivation_in_flight(monkeypatch):
    """The real rebuild path must refuse to contend with an in-flight
    derivation for the write gate (the 2026-08-07 freeze interleaving)."""
    import sqlite3

    from topos.enrichment.pipeline_activity import derivation_in_flight, reset_for_tests
    from topos.storage.db.write_gate import WriteGateDeferred

    reset_for_tests()
    db = sqlite3.connect(":memory:")
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr("topos.core.state.close_thread_db_connection", lambda: None)

    with derivation_in_flight():
        with pytest.raises(WriteGateDeferred):
            graph_refresh._default_rebuild()


@pytest.mark.asyncio
async def test_mark_graph_dirty_persists_off_the_event_loop(monkeypatch):
    """The dirty-generation bump takes the write gate; from async code it must
    run on an executor thread, never on the loop (2026-08-07 freeze site)."""
    import asyncio

    monkeypatch.setenv("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", "60")
    graph_refresh.reset_for_tests(rebuild_fn=lambda: None)

    seen = {}
    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: object()
    )
    monkeypatch.setattr(
        graph_refresh,
        "_persist_dirty_generation",
        lambda conn: seen.setdefault("thread", threading.current_thread()),
    )

    loop_thread = threading.current_thread()
    graph_refresh.mark_graph_dirty()
    deadline = asyncio.get_running_loop().time() + 5
    while "thread" not in seen and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)

    assert seen.get("thread") is not None, "dirty generation was never persisted"
    assert seen["thread"] is not loop_thread, "persistence ran on the event-loop thread"


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



def test_no_caller_wraps_the_rebuild_in_the_gate():
    """The rebuild gates its own write phases (M2.2). The gate is a reentrant
    RLock, so an outer with_db_write() at any caller would silently reinstate
    the whole-rebuild exclusive hold (~120s observed 2026-08-07) with no test
    failing anywhere else. Checked at every layer the call now passes through:
    the refresher's rebuild fn and the subprocess dispatcher's in-process
    fallback."""
    import ast
    import inspect
    import textwrap

    from topos.features.entities import rebuild_subprocess

    def _gate_wraps_rebuild(fn, callee_names):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))

        class _Finder(ast.NodeVisitor):
            wrapped = False

            def visit_With(self, node: ast.With) -> None:
                gate_names = set()
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call):
                        f = expr.func
                        gate_names.add(getattr(f, "id", None) or getattr(f, "attr", None))
                body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                if "with_db_write" in gate_names and any(c in body for c in callee_names):
                    self.wrapped = True
                self.generic_visit(node)

        finder = _Finder()
        finder.visit(tree)
        return finder.wrapped

    assert not _gate_wraps_rebuild(
        graph_refresh._default_rebuild, ("run_graph_rebuild", "rebuild_entity_graph")
    ), "_default_rebuild wraps the rebuild in with_db_write — reentrant gate, whole-rebuild hold returns"
    assert not _gate_wraps_rebuild(
        rebuild_subprocess.run_graph_rebuild, ("rebuild_entity_graph", "rebuild_in_subprocess")
    ), "run_graph_rebuild wraps the rebuild in with_db_write — reentrant gate, whole-rebuild hold returns"


def test_persist_skips_a_connection_shared_across_threads():
    """A worker thread must not write on a connection it does not own.

    mark_graph_dirty persists from an executor thread on the promise that the
    thread gets its OWN connection. That promise is void for an in-memory
    database (core.state hands out the owner's, since a per-thread copy would
    be empty) and for a handle a test injected. Writing anyway races the
    thread already using it: one sqlite3.Connection carries one transaction
    state, and the write gate serializes writers, not readers. That corrupted
    the transaction state and segfaulted the CI lane from this call site.
    """
    import sqlite3
    import threading

    from topos.storage.db.migrations import apply_all_migrations

    shared = sqlite3.connect(":memory:", check_same_thread=False)
    apply_all_migrations(shared)
    before = shared.execute(
        "SELECT dirty_generation FROM graph_materialization_state WHERE id=1"
    ).fetchone()[0]

    errors: list[BaseException] = []

    def _write_from_a_thread_that_does_not_own_it() -> None:
        try:
            graph_refresh._persist_dirty_generation(shared)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=_write_from_a_thread_that_does_not_own_it)
    worker.start()
    worker.join(timeout=10)

    assert not errors, f"persist raised on a shared connection: {errors}"
    after = shared.execute(
        "SELECT dirty_generation FROM graph_materialization_state WHERE id=1"
    ).fetchone()[0]
    assert after == before, "persist wrote on a connection shared with another thread"
