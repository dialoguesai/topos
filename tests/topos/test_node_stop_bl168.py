"""BL-168: a node told to stop stops its own work at once, and the whole stop is bounded.

On 10 Oct 2026 (1.5.3) the port closed at the SIGTERM and the node ran on for 14 minutes: the pipeline worker kept
running the job it held, the permissions runtime's background threads kept making model calls, and the
control-plane client (stopped last of all in shutdown_event) stayed connected, dropping relayed asks as "saturation".
uvicorn reached shutdown_event only when its unbounded drain of in-flight requests ended.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos import node_stop
from topos.pipeline import job_runner
from topos.pipeline.job_store import claim_next_job, enqueue_job, get_job
from topos.runtime_shutdown import begin_runtime, current_generation
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture(autouse=True)
def _fresh_run(monkeypatch):
    """Each test runs in its own runtime generation, and no test can arm the forced exit."""
    from topos.features.entities import rebuild_subprocess

    rebuild_subprocess.allow_rebuild_children()
    monkeypatch.setenv("TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS", "0")
    monkeypatch.setattr(node_stop, "_stop_begun", threading.Event())
    begin_runtime("test")
    yield
    begin_runtime("test")
    node_stop.set_background_reaper(None)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    yield db


def _stub_rebuild(monkeypatch, order):
    monkeypatch.setattr("topos.features.entities.rebuild_subprocess.stop_rebuild_children",
                        lambda grace_s=2.0: order.append("rebuild") or 0)


class _Client:
    def __init__(self, order):
        self.order = order

    async def stop(self):
        self.order.append("control_plane")


@pytest.mark.asyncio
async def test_the_stop_closes_the_control_plane_client_first(monkeypatch):
    """Asks relayed while the node goes away must fail at the control plane at once, not queue on a dying node."""
    from topos.core import state
    from topos.permissions_v2 import runtime as permissions_runtime

    order: list[str] = []

    async def _park(generation=None):
        order.append("ingestion")

    async def _reap():
        order.append("background")

    monkeypatch.setattr(state, "control_plane_client", _Client(order), raising=False)
    monkeypatch.setattr(job_runner, "park_pipeline_worker", _park)
    monkeypatch.setattr(permissions_runtime, "stop_background_work", lambda: order.append("permissions"))
    _stub_rebuild(monkeypatch, order)
    node_stop.bind_loop(asyncio.get_running_loop())
    node_stop.set_background_reaper(_reap)

    await node_stop.stop_now(current_generation())
    assert order == ["control_plane", "rebuild", "ingestion", "permissions", "background"], order
    await node_stop.stop_now(current_generation())
    assert len(order) == 5, "a second stop_now in the same run ran the steps again"


@pytest.mark.asyncio
async def test_a_signal_begins_the_stop_on_the_app_loop_without_waiting_for_the_drain(monkeypatch):
    """begin_stop is a signal listener: it only schedules. The steps then run on the app loop by themselves, while
    uvicorn would still be draining requests, and shutdown_event's stop_now joins the same task."""
    from topos.core import state
    from topos.permissions_v2 import runtime as permissions_runtime

    order: list[str] = []

    async def _park(generation=None):
        order.append(("ingestion", generation))

    monkeypatch.setattr(state, "control_plane_client", _Client(order), raising=False)
    monkeypatch.setattr(job_runner, "park_pipeline_worker", _park)
    monkeypatch.setattr(permissions_runtime, "stop_background_work", lambda: order.append("permissions"))
    _stub_rebuild(monkeypatch, order)
    node_stop.bind_loop(asyncio.get_running_loop())
    run = current_generation()

    assert node_stop.begin_stop("signal:SIGTERM") is True
    assert node_stop.begin_stop("signal:SIGTERM") is False, "a second signal began a second stop"
    assert run.is_stop_requested(), "a tray Quit (no signal) must stop the run's workers too"
    assert order == [], "begin_stop ran work inside the signal handler"
    deadline = time.monotonic() + 5
    while "permissions" not in order and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert order == ["control_plane", "rebuild", ("ingestion", run), "permissions"], order
    await node_stop.stop_now(run)
    assert order.count("control_plane") == 1


@pytest.mark.asyncio
async def test_parking_requeues_the_held_job_and_nothing_restarts_the_queue(conn):
    """The job a worker holds goes back to the queue (resumable, not failed, not lost), and for the rest of the run
    neither a request (start_pipeline_worker) nor the loop claims again. The next run is not parked."""
    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def _exec(payload: dict) -> dict:
        ran.append(payload["marker"])
        started.set()
        await release.wait()
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    enqueue_job(conn, kind="file_ingestion", payload={"marker": "held"}, job_id="held-1")
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _exec}), \
            patch.object(job_runner, "_long_running_kinds", lambda: ["no-such-kind"]):
        job_runner.start_pipeline_worker(lambda: conn)
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            assert get_job(conn, "held-1")["status"] == "running"

            await asyncio.wait_for(job_runner.park_pipeline_worker(current_generation()), timeout=10)
            job = get_job(conn, "held-1")
            assert job["status"] == "queued", job
            assert job["lease_owner"] is None and int(job.get("attempts") or 0) <= 1
            assert job_runner.pipeline_parked()

            job_runner.start_pipeline_worker(lambda: conn)   # a request during the drain
            assert job_runner._worker_task is None and job_runner._long_worker_task is None
            assert claim_next_job(conn, lease_owner="probe", kinds=["no-such-kind"]) is None
            assert ran == ["held"]
        finally:
            await job_runner.stop_pipeline_worker()
    begin_runtime("next start")
    assert not job_runner.pipeline_parked(), "the next run started parked"


@pytest.mark.asyncio
async def test_a_parked_worker_loop_claims_nothing(conn):
    """Even a loop that survived the cancel (or was started before the park) idles while parked."""
    ran: list[str] = []

    async def _exec(payload: dict) -> dict:
        ran.append("ran")
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    enqueue_job(conn, kind="file_ingestion", payload={}, job_id="parked-1")
    job_runner._parked_generation = current_generation()
    try:
        with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _exec}):
            task = asyncio.get_running_loop().create_task(job_runner._worker_loop(lambda: conn))
            await asyncio.sleep(1.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert ran == [] and get_job(conn, "parked-1")["status"] == "queued"
    finally:
        job_runner._parked_generation = None


def test_the_permissions_runtime_stops_its_background_work_but_keeps_its_lock():
    """Catch-up passes, the refresh loop, the rebuild queue and answer jobs stop; the single-process lock stays held
    until the process exits, so a new node cannot open the same databases while this one still writes."""
    from topos.permissions_v2.runtime import Runtime

    closed: list[str] = []

    def _closer(name):
        return SimpleNamespace(close=lambda: closed.append(name))

    lock = SimpleNamespace(close=lambda: closed.append("lock"))
    fake = SimpleNamespace(_sweeper_stop=threading.Event(), _refresh=_closer("refresh"),
                           _index_rebuilds=_closer("rebuilds"), _automatic_reviews=_closer("assessment"),
                           _answers=_closer("answers"), lock_file=lock)
    Runtime.stop_background(fake)
    assert fake._sweeper_stop.is_set()
    assert closed == ["refresh", "rebuilds", "assessment", "answers"], closed
    fake.stop_background = lambda: Runtime.stop_background(fake)
    Runtime.close(fake)
    assert closed[-1] == "lock"


def test_stop_background_work_loads_no_runtime_and_ends_the_start_up_thread(monkeypatch):
    from topos.permissions_v2 import refresh_loop
    from topos.permissions_v2 import runtime as permissions_runtime

    stopped: list[str] = []
    monkeypatch.setattr(refresh_loop, "stop_startup", lambda: stopped.append("startup"))
    monkeypatch.setattr(permissions_runtime, "_runtime", None)
    monkeypatch.setattr(permissions_runtime, "load_runtime", lambda *a, **k: pytest.fail("loaded a runtime"))
    assert permissions_runtime.stop_background_work() is False
    fake = SimpleNamespace(stop_background=lambda: stopped.append("runtime"))
    monkeypatch.setattr(permissions_runtime, "_runtime", fake)
    assert permissions_runtime.stop_background_work() is True
    assert stopped == ["startup", "startup", "runtime"]


def test_the_assessment_worker_stops_between_model_calls():
    """The catch-up pass is the node's own model work: closing it ends the pass after the call in flight, and what
    it published stays (durable checkpoints)."""
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker

    worker = AutomaticReviewWorker.__new__(AutomaticReviewWorker)
    worker._stop = threading.Event()
    worker.close()
    assert worker._stop.is_set()


@pytest.mark.asyncio
async def test_shutdown_event_closes_the_control_plane_client_before_reaping(monkeypatch, tmp_path):
    from topos import app as app_module
    from topos.core import state
    from topos.testing.lifespan import LifespanManager

    monkeypatch.setenv("TOPOS_KEY", "test-key")
    monkeypatch.setenv("CONTROL_PLANE_URL", "")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "engine.db"))
    order: list[str] = []
    real_reap = app_module._reap_rebuild_children

    async def _reap(*a, **k):
        order.append("rebuild")
        await real_reap(*a, **k)

    monkeypatch.setattr(app_module, "_reap_rebuild_children", _reap)
    async with LifespanManager(app_module.app):
        monkeypatch.setattr(state, "control_plane_client", _Client(order), raising=False)
    assert order and order[0] == "control_plane", order
    assert order.count("control_plane") == 1, order


def test_uvicorn_is_built_with_a_bounded_drain(monkeypatch):
    monkeypatch.delenv("TOPOS_SHUTDOWN_DRAIN_SECONDS", raising=False)
    assert node_stop.uvicorn_options() == {"timeout_graceful_shutdown": 10}
    monkeypatch.setenv("TOPOS_SHUTDOWN_DRAIN_SECONDS", "3")
    assert node_stop.uvicorn_options() == {"timeout_graceful_shutdown": 3}
    import inspect

    from topos.cli import commands, tray

    assert "node_stop.uvicorn_options()" in inspect.getsource(commands)
    assert "node_stop.uvicorn_options()" in inspect.getsource(tray.serve_with_tray)


def test_the_forced_exit_names_the_threads_it_did_not_wait_for(caplog):
    exits: list[int] = []
    blocker = threading.Event()
    thread = threading.Thread(target=blocker.wait, name="still-draining", daemon=True)
    thread.start()
    try:
        with caplog.at_level("WARNING", logger="topos.node_stop"):
            node_stop._exit_after(60, exit=exits.append, sleep=lambda _s: None)
    finally:
        blocker.set()
    assert exits == [0]
    assert "still-draining" in caplog.text and "exiting now" in caplog.text


def test_no_forced_exit_unless_a_serving_entry_point_armed_it(monkeypatch):
    """Tests and CLI commands never call enable_exit_deadline, so a stop there can never kill the process."""
    monkeypatch.setenv("TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS", "0")
    assert node_stop.enable_exit_deadline() is False


_SERVER = textwrap.dedent(
    """
    import asyncio, sys, time
    import uvicorn
    from topos import node_stop
    from topos.runtime_shutdown import add_shutdown_listener, install_shutdown_signal_hooks

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    install_shutdown_signal_hooks()
                    node_stop.bind_loop(asyncio.get_running_loop())
                    add_shutdown_listener(node_stop.begin_stop)
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    if sys.argv[2] == "shutdown":
                        # A shutdown step that never ends (the node's are bounded; this one is not).
                        print("shutdown-hangs", flush=True)
                        await asyncio.to_thread(time.sleep, 600)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        # A request whose work never ends and cannot be cancelled: a thread, as the node's jobs are.
        print("request-in-flight", flush=True)
        await asyncio.to_thread(time.sleep, 600)

    node_stop.enable_exit_deadline()
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning", **node_stop.uvicorn_options())
    """
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("hang", ["request", "shutdown"])
def test_a_sigterm_ends_the_process_within_the_bound_whatever_it_was_running(tmp_path, hang):
    """The 14-minute stop, end to end. ``request``: a request whose work never ends held uvicorn's drain, which had
    no bound, so lifespan shutdown (and the control-plane client's stop) never came; now uvicorn cancels it after
    the drain and the process dies of the SIGTERM as uvicorn re-raises it. ``shutdown``: a step after the drain
    never ends; the forced exit ends the process ``exit_deadline_seconds`` after the signal."""
    import socket
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    tree = os.path.dirname(os.path.dirname(os.path.abspath(node_stop.__file__)))   # the topos under test
    env = dict(os.environ, TOPOS_SHUTDOWN_DRAIN_SECONDS="1", TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS="4",
               PYTHONUNBUFFERED="1", PYTHONPATH=os.pathsep.join([tree, os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.Popen([sys.executable, str(script), str(port), hang], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        threading.Thread(target=lambda: _quietly(urllib.request.urlopen, f"http://127.0.0.1:{port}/", 60),
                         daemon=True).start()
        time.sleep(1.0)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        took = time.monotonic() - started
        output = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "request-in-flight" in output
    assert took < 12, (took, output[-800:])
    assert "timeout graceful shutdown exceeded" in output, output[-800:]
    if hang == "request":
        assert proc.returncode == -signal.SIGTERM, (proc.returncode, output[-800:])
        assert "exiting now" not in output
    else:
        assert "shutdown-hangs" in output and "exiting now" in output, output[-800:]
        assert proc.returncode == 0


def _quietly(fn, *args):
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        pass


def _returns_within(fn, seconds=2.0):
    done = threading.Event()
    result = []

    def run():
        result.append(fn())
        done.set()
    threading.Thread(target=run, daemon=True).start()
    return done.wait(seconds), result


def test_the_stop_never_waits_on_a_lock_the_interrupted_thread_holds():
    """R-N154 L4: the signal handler runs on the main thread, in the middle of whatever it was doing. Holding the
    generation registry's lock or a generation's own lock, as begin_runtime / request_stop do, must not hang the
    stop that interrupts it (simulated on one thread: hold the lock, then call what the handler calls)."""
    from topos import runtime_shutdown

    # Throwaway locks and generation: on a regression the stuck thread holds only these, never the process's own
    # (the registry lock is put back before any teardown can need it).
    real_lock = runtime_shutdown._GEN_LOCK
    throwaway = threading.Lock()

    def under_registry_lock():
        with throwaway:
            return runtime_shutdown.current_generation() is not None and job_runner.pipeline_parked() is False

    runtime_shutdown._GEN_LOCK = throwaway
    try:
        finished, result = _returns_within(under_registry_lock)
    finally:
        runtime_shutdown._GEN_LOCK = real_lock
    assert finished and result == [True], "current_generation waited on a lock its own thread held"

    def under_generation_lock():
        generation = runtime_shutdown.RuntimeGeneration(10_000)
        with generation._lock:
            generation.request_stop("signal:SIGTERM")
        return generation.is_stop_requested()

    finished, result = _returns_within(under_generation_lock)
    assert finished and result == [True], "request_stop waited on a lock its own thread held"
