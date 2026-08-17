"""Guards for the startup background work that used to outlive its app.

The failure these prevent, end to end: app startup launched fire-and-forget
tasks and an upgrade-runner thread, shutdown reaped none of them, so in a
process that starts the app more than once (any test module using
``LifespanManager``) an earlier instance's runner was still writing while a
later instance ran its startup migrations. The migration took the process-wide
write gate and then blocked on SQLite, pinning the gate for the full 30s
``busy_timeout`` — the same 30s as the test lifespan budget, so it surfaced as
"App startup did not complete within 30s" on whichever test happened to be
starting, never on the one at fault.

Observed in CI as, on one run:

    [WRITE_GATE] slow section at __init__.py:274 in ensure_migrations_applied:
        waited=0.0s held=30.0s
    FAILED tests/topos/test_ingestion_sources.py::test_sync_imessage_returns_ok_or_error
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from topos.storage.db.write_gate import (
    bounded_busy_timeout,
    db_write_lock,
    describe_gate_holder,
    with_db_write,
)


# --- the upgrade runner is stoppable ----------------------------------------


def _plan(monkeypatch, *, fresh_install=False, steps=(("backfill-attention-triage",))):
    monkeypatch.setattr(
        "topos.upgrades.runner.plan_upgrade",
        lambda _c: {
            "shipped": "1.3.0",
            "baseline": "1.2.7",
            "fresh_install": fresh_install,
            "steps": [{"id": s} for s in steps],
        },
    )


def test_stop_event_during_grace_prevents_the_upgrade(monkeypatch):
    """Set mid-grace, the runner must exit without touching the database.

    The grace wait was a plain ``time.sleep``: uninterruptible, so the thread
    stayed alive for the whole window after its app had shut down and then ran
    migrations against a database the next app was already using.
    """
    from topos.upgrades.runner import start_background

    ran = threading.Event()
    monkeypatch.setattr(
        "topos.upgrades.runner.run_pending_upgrades",
        lambda *a, **k: ran.set() or {"steps_run": 0, "steps_failed": 0},
    )
    _plan(monkeypatch)

    ready, stop = threading.Event(), threading.Event()
    thread = start_background(
        None, ready_event=ready, ui_grace_s=5.0, ready_timeout_s=2.0, stop_event=stop
    )
    assert thread is not None
    ready.set()
    time.sleep(0.2)  # now inside the 5s grace
    assert not ran.is_set()

    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "grace wait ignored the stop event"
    assert not ran.is_set(), "upgrade ran despite being stopped during grace"


def test_stop_event_during_ready_wait_prevents_the_upgrade(monkeypatch):
    """Stopped while waiting on UI readiness: exit now, not at ready_timeout."""
    from topos.upgrades.runner import start_background

    ran = threading.Event()
    monkeypatch.setattr(
        "topos.upgrades.runner.run_pending_upgrades",
        lambda *a, **k: ran.set() or {"steps_run": 0, "steps_failed": 0},
    )
    _plan(monkeypatch)

    ready, stop = threading.Event(), threading.Event()
    thread = start_background(
        None, ready_event=ready, ui_grace_s=0.0, ready_timeout_s=30.0, stop_event=stop
    )
    assert thread is not None
    time.sleep(0.2)  # waiting on `ready`, which never fires

    started = time.monotonic()
    stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "ready wait ignored the stop event"
    # The point of polling in slices: without it this waits out ready_timeout.
    assert time.monotonic() - started < 3.0
    assert not ran.is_set()


def test_stamp_thread_respects_the_stop_event(monkeypatch):
    """The fresh-install branch is a second thread, and it needs the check too."""
    from topos.upgrades.runner import start_background

    ran = threading.Event()
    release = threading.Event()

    def _slow_run(*_a, **_k):
        release.wait(timeout=5.0)
        ran.set()
        return {"steps_run": 0, "steps_failed": 0}

    monkeypatch.setattr("topos.upgrades.runner.run_pending_upgrades", _slow_run)
    _plan(monkeypatch, fresh_install=True, steps=())

    stop = threading.Event()
    stop.set()  # already shutting down when the thread starts
    thread = start_background(None, stop_event=stop)
    assert thread is not None
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert not ran.is_set(), "stamp ran despite a stop event set before it started"
    release.set()


def test_runner_without_a_stop_event_still_works(monkeypatch):
    """stop_event is optional — callers that pass none keep the old behaviour."""
    from topos.upgrades.runner import start_background

    ran = threading.Event()
    monkeypatch.setattr(
        "topos.upgrades.runner.run_pending_upgrades",
        lambda *a, **k: ran.set() or {"steps_run": 0, "steps_failed": 0},
    )
    _plan(monkeypatch)

    ready = threading.Event()
    thread = start_background(None, ready_event=ready, ui_grace_s=0.01, ready_timeout_s=2.0)
    assert thread is not None
    ready.set()
    assert ran.wait(timeout=3.0), "upgrade should still run when no stop_event is given"
    thread.join(timeout=2.0)


def test_run_pending_upgrades_stops_at_a_step_boundary(monkeypatch):
    """A run already under way must end at the next step, not finish the plan.

    Interrupting the waits is not enough on its own: in CI the ready-timeout and
    grace (80s together) do elapse during a long suite, so the leaked runner
    reaches `run_pending_upgrades` and is mid-plan when the next app starts.
    """
    from topos.upgrades import runner as runner_mod

    stop = threading.Event()
    executed: list[str] = []

    def _executor(step, _conn):
        executed.append(str(step["id"]))
        stop.set()  # shutdown lands while step one is running
        return {"ok": True}

    monkeypatch.setattr(runner_mod, "_enabled", lambda: True)
    monkeypatch.setattr(
        runner_mod,
        "plan_upgrade",
        lambda _c, shipped=None: {
            "shipped": "1.3.0",
            "baseline": "1.2.7",
            "fresh_install": False,
            "steps": [{"id": "one", "kind": "k"}, {"id": "two", "kind": "k"}],
        },
    )
    monkeypatch.setattr(runner_mod, "_effective_status", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_ledger_set", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_stamp_baseline", lambda *a, **k: None)

    result = runner_mod.run_pending_upgrades(
        None, executors={"k": _executor}, stop_event=stop
    )

    assert executed == ["one"], f"ran past the stop: {executed}"
    assert result["stopped_early"] is True
    assert not result["baseline_advanced"], "an interrupted plan must not stamp"


def test_run_pending_upgrades_without_stop_event_runs_every_step(monkeypatch):
    """The stop check must not change behaviour for callers that pass nothing."""
    from topos.upgrades import runner as runner_mod

    executed: list[str] = []
    monkeypatch.setattr(runner_mod, "_enabled", lambda: True)
    monkeypatch.setattr(
        runner_mod,
        "plan_upgrade",
        lambda _c, shipped=None: {
            "shipped": "1.3.0",
            "baseline": "1.2.7",
            "fresh_install": False,
            "steps": [{"id": "one", "kind": "k"}, {"id": "two", "kind": "k"}],
        },
    )
    monkeypatch.setattr(runner_mod, "_effective_status", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_ledger_set", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_stamp_baseline", lambda *a, **k: None)

    result = runner_mod.run_pending_upgrades(
        None, executors={"k": lambda s, _c: executed.append(str(s["id"])) or {"ok": True}}
    )
    assert executed == ["one", "two"]
    assert result["stopped_early"] is False


# --- the pipeline worker is stoppable ---------------------------------------


@pytest.mark.asyncio
async def test_stop_pipeline_worker_clears_the_global(monkeypatch):
    """A stopped worker must free the slot for the next app instance.

    ``start_pipeline_worker`` no-ops while ``_worker_task`` is not ``done()``,
    and a task abandoned on a closed loop never becomes done — so before this,
    the first app in a process owned the worker forever and every later one
    silently ran without one.
    """
    from topos.pipeline import job_runner

    monkeypatch.setattr(job_runner, "_enabled", lambda: True)

    async def _idle(_factory):
        await asyncio.sleep(3600)

    monkeypatch.setattr(job_runner, "_worker_loop", _idle)

    job_runner.start_pipeline_worker(lambda: None)
    assert job_runner._worker_task is not None

    await job_runner.stop_pipeline_worker()
    assert job_runner._worker_task is None, "the global still names a dead worker"

    # The slot is genuinely reusable.
    job_runner.start_pipeline_worker(lambda: None)
    assert job_runner._worker_task is not None
    await job_runner.stop_pipeline_worker()


@pytest.mark.asyncio
async def test_stop_pipeline_worker_is_safe_when_none_running():
    from topos.pipeline import job_runner

    await job_runner.stop_pipeline_worker()  # must not raise
    assert job_runner._worker_task is None


# --- shutdown reaps what startup spawned ------------------------------------

# `app_module.state`, never a fresh `from topos.core import state`. Tests that
# reload core modules fork the module identity, so the two can be different
# objects holding different sets — the app writes to one and an unwary assert
# reads the other. Whatever the app resolves is by definition the right one.


@pytest.mark.asyncio
async def test_reap_background_tasks_cancels_and_empties():
    from topos import app as app_module

    state = app_module.state
    started = asyncio.Event()

    async def _never_finishes():
        started.set()
        await asyncio.sleep(3600)

    task = app_module._spawn_background(_never_finishes(), name="test-task")
    await started.wait()
    assert task in state.background_tasks

    await app_module._reap_background_tasks()
    assert task.cancelled() or task.done()
    assert not state.background_tasks, "shutdown left background tasks tracked"


@pytest.mark.asyncio
async def test_spawn_background_untracks_on_normal_completion():
    """The registry must not grow without bound during a long-lived process."""
    from topos import app as app_module

    state = app_module.state

    async def _quick():
        return None

    task = app_module._spawn_background(_quick(), name="test-quick")
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in state.background_tasks


@pytest.mark.asyncio
async def test_reap_upgrade_runner_sets_stop_and_joins():
    from topos import app as app_module

    state = app_module.state
    stop = threading.Event()
    exited = threading.Event()

    def _wait_for_stop():
        stop.wait(timeout=10.0)
        exited.set()

    thread = threading.Thread(target=_wait_for_stop, name="topos-upgrade-runner", daemon=True)
    thread.start()
    state.upgrade_runner_stop = stop
    state.upgrade_runner_thread = thread

    await app_module._reap_upgrade_runner(timeout_s=5.0)

    assert exited.is_set(), "shutdown did not signal the upgrade runner"
    assert not thread.is_alive()
    assert state.upgrade_runner_thread is None
    assert state.upgrade_runner_stop is None


@pytest.mark.asyncio
async def test_reap_upgrade_runner_is_safe_with_no_runner():
    from topos import app as app_module

    state = app_module.state
    state.upgrade_runner_stop = None
    state.upgrade_runner_thread = None
    await app_module._reap_upgrade_runner(timeout_s=1.0)  # must not raise


# --- the lock-order inversion ------------------------------------------------


def _locked_db(tmp_path):
    """A database whose write lock is held by a second, ungated connection."""
    path = tmp_path / "locked.db"
    holder = sqlite3.connect(str(path))
    holder.execute("PRAGMA journal_mode=wal")
    holder.execute("CREATE TABLE seed(x)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO seed VALUES (1)")
    return path, holder


def test_blocked_migration_does_not_pin_the_write_gate(tmp_path):
    """THE regression guard for the CI flake.

    A migration that cannot get SQLite's write lock must release the process-wide
    gate between attempts. Holding it across the full busy_timeout is what
    blocked an unrelated app startup for 30s and failed its lifespan budget.
    """
    from topos.storage.db.migrations import _apply_one_migration

    path, holder = _locked_db(tmp_path)
    try:
        # check_same_thread=False: the migration runs on the worker below, and
        # production opens its connections the same way (core/state.py).
        migrating = sqlite3.connect(str(path), check_same_thread=False)
        migrating.execute("PRAGMA busy_timeout=30000")  # the production default
        spec = SimpleNamespace(
            id="test-blocked", fn=lambda c: c.execute("CREATE TABLE later(y)")
        )

        finished = threading.Event()

        def _run():
            try:
                _apply_one_migration(migrating, spec)
            except Exception:  # noqa: BLE001 — outcome is asserted via the gate
                pass
            finally:
                finished.set()

        worker = threading.Thread(target=_run, name="test-migration", daemon=True)
        worker.start()
        try:
            time.sleep(0.5)  # let it get into its blocked-and-retrying cycle
            gate = db_write_lock()
            acquired = gate.acquire(timeout=5.0)
            if acquired:
                gate.release()
            assert acquired, (
                "the migration pinned the write gate while blocked on SQLite; "
                "every other writer in the process would stall for busy_timeout"
            )
        finally:
            holder.rollback()  # unblock, so the migration can complete
            finished.wait(timeout=20.0)
            worker.join(timeout=5.0)
    finally:
        holder.close()


def test_blocked_migration_eventually_succeeds(tmp_path):
    """Releasing the gate between attempts must not turn a wait into a failure."""
    from topos.storage.db.migrations import _apply_one_migration

    path, holder = _locked_db(tmp_path)
    try:
        migrating = sqlite3.connect(str(path), check_same_thread=False)
        migrating.execute("PRAGMA busy_timeout=30000")
        spec = SimpleNamespace(
            id="test-eventual", fn=lambda c: c.execute("CREATE TABLE later(y)")
        )
        outcome = {}

        def _run():
            try:
                _apply_one_migration(migrating, spec)
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        time.sleep(0.3)
        holder.rollback()  # writer goes away; the retry should now win
        worker.join(timeout=20.0)
        assert outcome.get("ok"), f"migration never completed: {outcome.get('error')!r}"
        assert migrating.execute(
            "SELECT name FROM sqlite_master WHERE name='later'"
        ).fetchone() is not None
    finally:
        holder.close()


def test_non_busy_migration_errors_are_not_retried(tmp_path):
    """Only lock contention retries; a real failure must surface immediately."""
    from topos.storage.db.migrations import _apply_one_migration

    conn = sqlite3.connect(str(tmp_path / "plain.db"))
    calls = []

    def _boom(_c):
        calls.append(1)
        raise ValueError("not a lock problem")

    with pytest.raises(ValueError):
        _apply_one_migration(conn, SimpleNamespace(id="test-boom", fn=_boom))
    assert len(calls) == 1, "a non-busy error must not be retried"


def test_bounded_busy_timeout_restores_the_previous_value(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "pragma.db"))
    conn.execute("PRAGMA busy_timeout=30000")
    before = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    with bounded_busy_timeout(conn, 250):
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 250

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == before


def test_bounded_busy_timeout_restores_after_an_exception(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "pragma2.db"))
    conn.execute("PRAGMA busy_timeout=30000")

    with pytest.raises(RuntimeError):
        with bounded_busy_timeout(conn, 250):
            raise RuntimeError("boom")

    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


# --- the failure names its cause --------------------------------------------


def test_describe_gate_holder_names_the_site_and_thread():
    assert describe_gate_holder() == "not held"
    seen = {}

    def _hold():
        with with_db_write():
            seen["desc"] = describe_gate_holder()

    worker = threading.Thread(target=_hold, name="test-holder", daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert "test-holder" in seen["desc"], seen["desc"]
    assert "not held" not in seen["desc"]
    assert describe_gate_holder() == "not held", "holder outlived its section"


def test_describe_gate_holder_survives_reentrant_acquisition():
    """The gate is an RLock; an inner release must not clear a live hold."""
    with with_db_write():
        outer = describe_gate_holder()
        with with_db_write():
            pass
        assert describe_gate_holder() != "not held", (
            "a nested section's exit cleared the holder while the outer one "
            "still held the gate"
        )
        assert describe_gate_holder().split(" for ")[0] == outer.split(" for ")[0]
    assert describe_gate_holder() == "not held"


def test_lifespan_timeout_message_points_at_the_write_gate():
    """The hint used to send readers to the event loop, which was never it."""
    from topos.testing.lifespan import _TIMEOUT_HINT, _describe_gate

    rendered = _TIMEOUT_HINT.format(seconds=30, holder=_describe_gate())
    assert "WRITE GATE" in rendered
    assert "busy_timeout" in rendered
    assert "leaked background work" in rendered
