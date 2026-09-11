"""Subprocess entity-graph rebuild (GIL-starvation fix, 2026-08-08).

A forced rebuild starved the node's event loop for ~103s with ZERO write-gate
warnings: the in-process compute (goal embeddings, role map, Louvain) held the
GIL, so the loop thread never ran — lock discipline can't fix scheduling.
``run_graph_rebuild`` therefore ships file-backed rebuilds to a child process;
these tests pin the dispatch rules, the child's verdict protocol, the advisory
single-flight lock, and — the point of it all — that the event loop keeps
serving while a rebuild runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from topos.features.entities import rebuild_subprocess
from topos.features.entities.rebuild_subprocess import (
    GraphRebuildStopped,
    GraphRebuildSubprocessError,
    _database_file,
    _parse_verdict,
    rebuild_in_subprocess,
    run_graph_rebuild,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import WriteGateDeferred


def _seed(conn: sqlite3.Connection) -> None:
    """Two entities co-mentioned on one record — enough for one edge."""
    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Bram", "person")
    for entity_id in (a, b):
        conn.execute(
            """
            INSERT INTO entity_mentions
                (mention_id, entity_id, record_id, source_id, canonical_table,
                 surface_text, confidence, event_at, created_at)
            VALUES (?, ?, 'rec1', 'imessage', 'conversation_messages',
                    'x', 0.9, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (f"m_{entity_id}", entity_id),
        )
    conn.commit()


@pytest.fixture()
def file_db(tmp_path):
    path = str(tmp_path / "graph.db")
    # check_same_thread=False matches production connections
    # (core.state.get_db_connection) and lets the async test use the conn from
    # an asyncio.to_thread worker.
    conn = sqlite3.connect(path, check_same_thread=False)
    apply_all_migrations(conn)
    _seed(conn)
    yield path, conn
    conn.close()


def test_database_file_resolution(file_db):
    path, conn = file_db
    assert _database_file(conn) == path
    mem = sqlite3.connect(":memory:")
    assert _database_file(mem) is None


def test_dispatch_uses_subprocess_for_file_db(file_db, monkeypatch):
    path, conn = file_db
    calls = []
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda db_path, **kw: calls.append(db_path) or {"edges_after": 1},
    )
    report = run_graph_rebuild(conn)
    assert calls == [path]
    assert report == {"edges_after": 1}


def test_dispatch_runs_in_process_for_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    _seed(conn)
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda *a, **kw: pytest.fail("in-memory database must rebuild in-process"),
    )
    report = run_graph_rebuild(conn)
    assert report["co_occurrence"] >= 1


def test_dispatch_kill_switch_forces_in_process(file_db, monkeypatch):
    _path, conn = file_db
    monkeypatch.setenv("TOPOS_GRAPH_REBUILD_SUBPROCESS", "off")
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda *a, **kw: pytest.fail("kill-switch must keep the rebuild in-process"),
    )
    report = run_graph_rebuild(conn)
    assert report["co_occurrence"] >= 1


def test_parse_verdict_ignores_stray_stdout():
    out = "loading model...\n{'not': 'json'}\n" + json.dumps(
        {"status": "ok", "report": {"edges_after": 3}}
    )
    assert _parse_verdict(out) == {"status": "ok", "report": {"edges_after": 3}}
    assert _parse_verdict("no verdict here") is None
    assert _parse_verdict(None) is None


def test_subprocess_rebuild_end_to_end(file_db):
    """Real child process: rebuilds the graph on its own connection and the
    parent sees the result through SQLite, not shared memory."""
    path, conn = file_db
    conn.execute("DELETE FROM entity_edges")
    conn.commit()

    report = rebuild_in_subprocess(path, timeout_s=180.0)

    assert report["co_occurrence"] >= 1
    assert report["edges_after"] >= 1
    active = conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE edge_type='co_occurrence' AND valid_to IS NULL"
    ).fetchone()[0]
    assert active >= 1, "parent connection must see the child's rebuilt edges"


def test_subprocess_rebuild_defers_when_lock_held(file_db):
    """Second rebuild steps aside (WriteGateDeferred) instead of interleaving."""
    fcntl = pytest.importorskip("fcntl")
    path, _conn = file_db
    with open(path + ".rebuild.lock", "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WriteGateDeferred):
            rebuild_in_subprocess(path, timeout_s=180.0)


def test_subprocess_missing_database_is_an_error(tmp_path):
    with pytest.raises(GraphRebuildSubprocessError):
        rebuild_in_subprocess(str(tmp_path / "nope.db"), timeout_s=180.0)


# --- the child never outlives the node (2026-09-11) --------------------------
#
# The node was stopped by a SIGTERM to its own pid only, nothing forwarded it,
# and eight node starts on 2026-09-08 found the previous node's rebuild child
# still running and holding the lock. These pin what ends that: a group kill,
# a stop at the signal, a stop in app shutdown, a closed spawn gate, and the
# child's own parent watch.

_posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


@pytest.fixture()
def isolated_children(monkeypatch):
    """A private child registry and an open spawn gate: a stop in one test can
    never reach a rebuild some other test left running, and vice versa."""
    monkeypatch.setattr(rebuild_subprocess, "_live", {})
    monkeypatch.setattr(rebuild_subprocess, "_stopped", set())
    monkeypatch.setattr(rebuild_subprocess, "_accepting", True)
    # As in the app, where a shutdown listener earns the child its own group.
    monkeypatch.setattr(rebuild_subprocess, "_child_gets_own_session", lambda: os.name == "posix")


# Stands in for the rebuild child. Its grandchild INHERITS the stdout/stderr
# pipes and IGNORES SIGTERM — multiprocessing's resource_tracker does both in a
# real rebuild — and is confirmed ready before its pid is published atomically.
_CHILD_WITH_GRANDCHILD = (
    "import os, subprocess, sys, time\n"
    "ready = sys.argv[1] + '.ready'\n"
    "g = subprocess.Popen([sys.executable, '-c',\n"
    "    'import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
    "    'open(sys.argv[1], \"w\").close(); time.sleep(60)', ready])\n"
    "while not os.path.exists(ready):\n"
    "    time.sleep(0.01)\n"
    "open(sys.argv[1] + '.tmp', 'w').write(str(g.pid))\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(60)\n"
)
_OK_VERDICT = "import json; print(json.dumps({'status': 'ok', 'report': {'edges_after': 0}}))"


def _substitute_child(monkeypatch, code, *args, on_spawn=None):
    """Make rebuild_in_subprocess spawn ``code`` instead of the real rebuild,
    keeping every Popen keyword it passes. Every other Popen in the process
    passes through untouched. Returns the keywords the rebuild spawn used."""
    real_popen = subprocess.Popen
    seen = {}

    def fake(cmd, **kwargs):
        if "topos.features.entities.rebuild_subprocess" not in cmd:
            return real_popen(cmd, **kwargs)
        seen.update(kwargs)
        seen["spawns"] = seen.get("spawns", 0) + 1
        proc = real_popen([sys.executable, "-c", code, *map(str, args)], **kwargs)
        if on_spawn is not None:
            on_spawn(proc)
        return proc

    monkeypatch.setattr(rebuild_subprocess.subprocess, "Popen", fake)
    return seen


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    return False


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _start_waiter(tmp_path, **kw):
    """Run rebuild_in_subprocess on a thread; return (thread, outcome dict)."""
    outcome = {}

    def _wait():
        try:
            outcome["report"] = rebuild_in_subprocess(str(tmp_path / "x.db"), **kw)
        except Exception as exc:  # noqa: BLE001 — the test inspects it
            outcome["error"] = exc

    waiter = threading.Thread(target=_wait, name="test-rebuild-waiter", daemon=True)
    waiter.start()
    return waiter, outcome


def test_child_is_spawned_in_its_own_session_and_told_its_parent(
    isolated_children, monkeypatch, tmp_path
):
    seen = _substitute_child(monkeypatch, _OK_VERDICT)
    assert rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=60.0) == {"edges_after": 0}
    assert seen["start_new_session"] is (os.name == "posix")
    assert seen["env"]["TOPOS_GRAPH_REBUILD_PARENT_PID"] == str(os.getpid())


@_posix_only
def test_timeout_kills_the_childs_own_children_too(isolated_children, monkeypatch, tmp_path):
    """Killing the child alone left its grandchild holding the pipes, so the
    drain after the kill waited on the grandchild (60s here)."""
    pidfile = tmp_path / "grandchild.pid"
    _substitute_child(monkeypatch, _CHILD_WITH_GRANDCHILD, pidfile)

    started = time.monotonic()
    with pytest.raises(GraphRebuildSubprocessError, match="exceeded"):
        rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=5.0)
    assert time.monotonic() - started < 20.0, "drain waited on the grandchild's pipes"
    assert _wait_until(pidfile.exists), "stand-in child never published its grandchild"
    assert _wait_until(lambda: _gone(int(pidfile.read_text()))), "grandchild survived the kill"


@_posix_only
def test_shutdown_stop_ends_the_child_and_frees_its_waiter(isolated_children, monkeypatch, tmp_path):
    """A rebuild awaited through asyncio.to_thread held asyncio.run's exit until
    the child finished on its own. The stop must end the whole group — including
    a member that ignores SIGTERM, which keeps the pipes open after the child
    itself is gone — and tell the waiter why."""
    pidfile = tmp_path / "grandchild.pid"
    _substitute_child(monkeypatch, _CHILD_WITH_GRANDCHILD, pidfile)
    waiter, outcome = _start_waiter(tmp_path, timeout_s=120.0)
    assert _wait_until(pidfile.exists)

    started = time.monotonic()
    assert rebuild_subprocess.stop_rebuild_children(grace_s=1.0) == 1
    waiter.join(timeout=15.0)
    assert not waiter.is_alive(), "waiter still blocked after the shutdown stop"
    assert time.monotonic() - started < 15.0
    assert isinstance(outcome.get("error"), GraphRebuildStopped), outcome
    assert "stopped by node shutdown" in str(outcome["error"])
    assert _wait_until(lambda: _gone(int(pidfile.read_text()))), "SIGTERM-ignoring member survived"
    assert rebuild_subprocess._live == {}, "registry kept a collected child"


@_posix_only
def test_signal_time_stop_frees_the_waiter_without_waiting(isolated_children, monkeypatch, tmp_path):
    """At the signal — before uvicorn drains an in-flight rebuild request — the
    stop must not block the signal handler, and must still end the group."""
    pidfile = tmp_path / "grandchild.pid"
    _substitute_child(monkeypatch, _CHILD_WITH_GRANDCHILD, pidfile)
    waiter, outcome = _start_waiter(tmp_path, timeout_s=120.0)
    assert _wait_until(pidfile.exists)

    started = time.monotonic()
    assert rebuild_subprocess.signal_rebuild_children(kill_after_s=1.0) == 1
    assert time.monotonic() - started < 0.5, "the signal-time stop blocked"
    waiter.join(timeout=15.0)
    assert not waiter.is_alive(), "waiter still blocked after the signal-time stop"
    assert isinstance(outcome.get("error"), GraphRebuildStopped), outcome
    assert _wait_until(lambda: _gone(int(pidfile.read_text())))
    # ...and nothing new may start in the drain window before shutdown_event.
    assert rebuild_subprocess._accepting is False, "the signal-time stop left the gate open"
    with pytest.raises(GraphRebuildStopped, match="not started"):
        rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=60.0)


def test_no_child_starts_once_the_node_is_stopping(isolated_children, monkeypatch, tmp_path):
    """A rebuild started after the last shutdown pass is one nobody reaps."""
    seen = _substitute_child(monkeypatch, _OK_VERDICT)
    rebuild_subprocess.stop_rebuild_children()
    with pytest.raises(GraphRebuildStopped, match="not started"):
        rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=60.0)
    assert "spawns" not in seen, "a child was spawned through a closed gate"

    rebuild_subprocess.allow_rebuild_children()  # the next app startup
    assert rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=60.0) == {"edges_after": 0}


@_posix_only
def test_a_stop_landing_mid_spawn_still_reaches_the_child(isolated_children, monkeypatch, tmp_path):
    """signal_rebuild_children takes no lock, so it can close the gate after the
    spawn's check and snapshot the registry before the child is in it. The
    spawn re-checks the gate and stops its own child."""
    pidfile = tmp_path / "grandchild.pid"

    def _signal_lands_now(_proc):
        # The real signal-time stop, landing after the spawn's gate check and
        # before the registry insert: its snapshot cannot see this child.
        assert rebuild_subprocess.signal_rebuild_children(kill_after_s=1.0) == 0

    _substitute_child(monkeypatch, _CHILD_WITH_GRANDCHILD, pidfile, on_spawn=_signal_lands_now)
    started = time.monotonic()
    with pytest.raises(GraphRebuildStopped):
        rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=120.0)
    assert time.monotonic() - started < 20.0


def test_a_stop_marks_only_children_still_registered(isolated_children, monkeypatch):
    """A child that finished on its own while a stop was under way is already
    collected; a mark left for its pid would outlive it and could later misname
    a new child that reused the pid."""
    monkeypatch.setattr(rebuild_subprocess, "_signal_tree", lambda *a, **k: None)

    class _Collected:
        pid = 987654

    rebuild_subprocess._stop_now([_Collected()], kill_after_s=None)
    assert rebuild_subprocess._stopped == set()

    rebuild_subprocess._stopped.add(123456)  # a mark nothing will ever clear
    rebuild_subprocess.allow_rebuild_children()
    assert rebuild_subprocess._stopped == set(), "the next run inherited a stale mark"


def test_without_a_shutdown_listener_the_child_stays_in_the_callers_group(
    isolated_children, monkeypatch, tmp_path
):
    """Its own group takes the child out of a terminal's Ctrl+C. A CLI command
    or script that never installed the node's stop then waited out the whole
    rebuild on Ctrl+C (fourth review) -- so no listener, no group of its own."""
    monkeypatch.setattr(rebuild_subprocess, "_child_gets_own_session", lambda: False)
    seen = _substitute_child(monkeypatch, _OK_VERDICT)
    assert rebuild_in_subprocess(str(tmp_path / "x.db"), timeout_s=60.0) == {"edges_after": 0}
    assert seen["start_new_session"] is False


@_posix_only
def test_a_child_in_the_callers_group_is_still_stoppable(isolated_children, monkeypatch, tmp_path):
    """Not a group leader, so killpg cannot reach it: the stop signals the child."""
    monkeypatch.setattr(rebuild_subprocess, "_child_gets_own_session", lambda: False)
    ready = tmp_path / "child.ready"
    _substitute_child(
        monkeypatch, "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(60)", ready
    )
    waiter, outcome = _start_waiter(tmp_path, timeout_s=120.0)
    assert _wait_until(ready.exists)
    assert rebuild_subprocess.stop_rebuild_children(grace_s=1.0) == 1
    waiter.join(timeout=15.0)
    assert not waiter.is_alive(), "a child in the caller's group was not stopped"
    assert isinstance(outcome.get("error"), GraphRebuildStopped), outcome


def test_only_a_registered_listener_earns_the_child_its_own_session(monkeypatch):
    from topos import runtime_shutdown

    monkeypatch.setattr(runtime_shutdown, "_HOOKS_INSTALLED", True)
    monkeypatch.setattr(runtime_shutdown, "_SHUTDOWN_LISTENERS", [])
    assert rebuild_subprocess._child_gets_own_session() is False
    runtime_shutdown.add_shutdown_listener(rebuild_subprocess.signal_rebuild_children)
    assert rebuild_subprocess._child_gets_own_session() is (os.name == "posix")
    # Tray mode: uvicorn runs off the main thread, so the hooks never install.
    monkeypatch.setattr(runtime_shutdown, "_HOOKS_INSTALLED", False)
    assert rebuild_subprocess._child_gets_own_session() is False


@_posix_only
def test_orphaned_child_exits_when_its_parent_dies(tmp_path):
    """The one guard that still works when the node itself is SIGKILLed: the
    child notices it was reparented and exits."""
    gpid = tmp_path / "g.pid"
    grandchild = (
        "import os, sys, time\n"
        "from topos.features.entities.rebuild_subprocess import _watch_parent\n"
        "_watch_parent(int(sys.argv[2]), poll_s=0.1)\n"
        "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(60)\n"
    )
    parent = (
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], str(os.getpid())],\n"
        "                 start_new_session=True)\n"
        "deadline = time.monotonic() + 20\n"
        "while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
    )
    subprocess.run([sys.executable, "-c", parent, grandchild, str(gpid)], check=True, timeout=30)
    assert gpid.exists(), "the child never started watching"
    assert _wait_until(lambda: _gone(int(gpid.read_text())), timeout=5.0), (
        "child outlived its parent"
    )


def test_child_main_arms_the_parent_watch(monkeypatch, tmp_path):
    armed = []
    monkeypatch.setattr(rebuild_subprocess, "_watch_parent", lambda pid, *a, **k: armed.append(pid))
    monkeypatch.setenv("TOPOS_GRAPH_REBUILD_PARENT_PID", "4242")
    assert rebuild_subprocess.main([str(tmp_path / "missing.db")]) == 1
    assert armed == [4242]


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_subprocess_rebuild(file_db):
    """The regression this whole module exists for: on 2026-08-08 a rebuild in
    asyncio.to_thread served ZERO loop iterations for ~103s (queued
    healthchecks flushed in one burst when it finished). With the compute in a
    child process the parent loop must keep ticking — sampled here the same
    way the live healthcheck poll caught the outage."""
    path, conn = file_db
    done = asyncio.Event()
    gaps = []

    async def tick():
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    sampler = asyncio.create_task(tick())
    worker_thread = []

    def _rebuild():
        worker_thread.append(threading.current_thread())
        return run_graph_rebuild(conn)

    try:
        report = await asyncio.to_thread(_rebuild)
    finally:
        done.set()
        await sampler

    assert report["edges_after"] >= 1
    assert worker_thread and worker_thread[0] is not threading.main_thread()
    # The 2026-08-08 failure was a ~103s gap; a healthy loop ticks every ~10ms.
    # 0.5s leaves room for CI scheduling jitter while still catching any
    # return of the convoy by two orders of magnitude.
    assert max(gaps) < 0.5, f"event loop starved during rebuild: worst gap {max(gaps):.2f}s"
