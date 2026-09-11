"""Run the entity-graph rebuild in a subprocess so the node keeps its GIL.

The M2.2/M2.3 work removed the rebuild's long write-gate holds, yet a forced
rebuild still blanked the event loop for ~103s on 2026-08-08 with ZERO
[WRITE_GATE] warnings: the worker thread's compute — sentence-transformers
goal-clustering embeddings on MPS, the per-record role map, Louvain — held the
GIL nearly continuously, so the loop thread simply never got scheduled (a
CPU-thread vs IO-thread convoy, not lock contention). No amount of gate
discipline fixes that; the compute has to leave the process.

So ``run_graph_rebuild`` dispatches: file-backed database → spawn
``python -m topos.features.entities.rebuild_subprocess <db_path>`` and let the
child do the whole rebuild on its own connection; in-memory database (tests) or
``TOPOS_GRAPH_REBUILD_SUBPROCESS=off`` → the old in-process call.

Cross-process coordination, deliberately minimal:

  * WRITES — the in-process write gate does not span processes, and it doesn't
    need to: SQLite/WAL serializes writers itself, and both sides run
    busy_timeout=30s (connection_tuning). Since M2.3 every rebuild write phase
    is a short bounded hold (worst observed 0.115s), and the child's long
    phases (embedding, role map, Louvain) are pure reads, which never block
    WAL writers. Worst case either side waits one short phase, far under the
    busy timeout.
  * REBUILD SINGLE-FLIGHT — an advisory flock on ``<db>.rebuild.lock``. A
    second rebuild (timer vs endpoint, or two node processes on one home dir)
    exits ``deferred`` instead of interleaving; the parent maps that to
    :class:`WriteGateDeferred` so the refresher re-arms exactly as it does for
    an in-flight derivation. The OS drops the lock if the child dies.
  * The child gets ``TOPOS_DATABASE_PATH`` pinned to the parent's resolved
    path, so any code that reaches for ``get_db_connection()`` instead of the
    passed connection still lands on the same file.

The child never outlives the node (2026-09-11). It used to: the node stops by a
SIGTERM to its own pid only (ToposShell's supervisor, then SIGKILL after 8s),
nothing forwarded it, and a child waited on from a daemon thread just kept
going — reparented, holding the rebuild lock with no timeout left to enforce,
writing to a database the next node was starting on. Eight node starts on
2026-09-08 found such an orphan still holding the lock. Now:

  * where a shutdown listener will stop it (the app), the child leads its own
    process group, so a kill reaches everything it spawned (multiprocessing's
    resource_tracker inherits its stdout/stderr and ignores SIGTERM); anywhere
    else — a CLI command, a script — it stays in the caller's group, where a
    terminal's Ctrl+C still reaches it;
  * a shutdown request stops every child at once (:func:`signal_rebuild_children`,
    a runtime_shutdown listener) — it has to be at the signal, not only in
    app shutdown, because uvicorn drains in-flight requests first and a rebuild
    awaited by POST /entities/graph/rebuild is one;
  * app shutdown stops them again and waits (:func:`stop_rebuild_children`),
    and no new child starts until the next app startup reopens the gate;
  * the child watches its parent and exits when it is gone — the only thing
    that still works when the node itself is SIGKILLed.

Cost accepted: the child cold-loads the embedding model each run (the node's
in-process cache doesn't carry over). Rebuilds are debounced to a few per hour
at most; a few seconds of model load per run buys a loop that never goes dark.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from ...storage.db.write_gate import WriteGateDeferred

logger = logging.getLogger("topos.features.entities.rebuild_subprocess")

_DEFAULT_TIMEOUT_S = 1800.0
# After a kill, how long to keep reading the child's pipes before giving up.
_DRAIN_AFTER_KILL_S = 10.0
# A stop is SIGTERM first; the group gets SIGKILL this long after a signal-time stop.
_KILL_AFTER_S = 3.0
# Env var carrying the node's pid to the child, for the parent watchdog.
_PARENT_PID_ENV = "TOPOS_GRAPH_REBUILD_PARENT_PID"
_PARENT_POLL_S = 1.0

# Children this process started and has not yet collected, by pid; the pids a
# stop signalled, so their waiter can say why they died; and whether a new child
# may start at all — closed by a shutdown, reopened by the next app startup.
_live_lock = threading.Lock()
_live: Dict[int, subprocess.Popen] = {}
_stopped: Set[int] = set()
_accepting = True


class GraphRebuildSubprocessError(RuntimeError):
    """The rebuild child crashed, hung, or returned no verdict."""


class GraphRebuildStopped(GraphRebuildSubprocessError):
    """The node is shutting down: the rebuild was stopped, or never started."""


def _subprocess_enabled() -> bool:
    return os.environ.get("TOPOS_GRAPH_REBUILD_SUBPROCESS", "on").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _timeout_s() -> float:
    try:
        return max(30.0, float(os.environ.get("TOPOS_GRAPH_REBUILD_TIMEOUT_S", _DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def _database_file(conn: sqlite3.Connection) -> Optional[str]:
    """The main database's backing file, or None for in-memory/temporary.

    Deliberately lets connection errors propagate: swallowing one here would
    silently fall back to the in-process rebuild — reinstating the exact GIL
    convoy this module removes — instead of surfacing the broken connection.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row:
        return None
    path = str(row[2] or "").strip()  # seq, name, file
    return path or None


def run_graph_rebuild(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Rebuild the entity graph: subprocess when file-backed, else in-process.

    The in-process fallback keeps ``:memory:`` tests and stripped environments
    working; it is also the kill-switch path (TOPOS_GRAPH_REBUILD_SUBPROCESS=off).
    Raises :class:`WriteGateDeferred` when another rebuild holds the advisory
    lock, so callers treat it exactly like stepping aside for a derivation.
    """
    db_path = _database_file(conn) if _subprocess_enabled() else None
    if not db_path:
        from .maintenance import rebuild_entity_graph

        return rebuild_entity_graph(conn)
    return rebuild_in_subprocess(db_path)


def _child_gets_own_session() -> bool:
    if os.name != "posix":
        return False
    from ...runtime_shutdown import will_notify

    return will_notify(signal_rebuild_children)


def rebuild_in_subprocess(db_path: str, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Spawn the rebuild child on ``db_path`` and return its report."""
    timeout = _timeout_s() if timeout_s is None else timeout_s
    db_path = os.path.abspath(db_path)
    env = dict(os.environ)
    # Pin any get_db_connection() fallback inside the child to the same file,
    # and keep the child from arming its own debounced refresher timers.
    env["TOPOS_DATABASE_PATH"] = db_path
    env["TOPOS_GRAPH_REFRESH"] = "off"
    env[_PARENT_PID_ENV] = str(os.getpid())
    cmd = [sys.executable, "-m", "topos.features.entities.rebuild_subprocess", db_path]

    # ``-m`` prepends the child's cwd to sys.path, so a node whose cwd holds a
    # DIFFERENT topos checkout would silently run that version's rebuild.
    # Spawning from this package's own parent pins the child to the exact code
    # the parent is executing.
    import topos as _topos_pkg

    code_root = os.path.dirname(os.path.dirname(os.path.abspath(_topos_pkg.__file__)))

    # Its own process group lets a stop reach everything the child spawned
    # (_signal_tree), but also takes it out of a terminal's Ctrl+C. So only
    # when a shutdown listener will stop it instead: the app, whose signal hooks
    # are installed. A CLI command or a script keeps the child in its own group,
    # where Ctrl+C reaches it as it always did.
    own_group = _child_gets_own_session()
    started = time.monotonic()
    # Check and spawn under the lock, so stop_rebuild_children either sees this
    # child or closed the gate before it could start.
    with _live_lock:
        if not _accepting:
            raise GraphRebuildStopped("node is shutting down; graph rebuild not started")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=code_root,
            start_new_session=own_group,
        )
        proc._topos_own_group = own_group  # type: ignore[attr-defined]
        _live[proc.pid] = proc
    if not _accepting:
        # signal_rebuild_children takes no lock, so it can close the gate after
        # the check above and snapshot the registry before the insert.
        _stop_now([proc], _KILL_AFTER_S)
    logger.info("graph rebuild subprocess started pid=%s db=%s", proc.pid, db_path)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_tree(proc, hard=True)
            _drain_after_kill(proc)
            raise GraphRebuildSubprocessError(
                f"graph rebuild subprocess pid={proc.pid} exceeded {timeout:.0f}s and was killed"
            ) from None
    finally:
        with _live_lock:
            _live.pop(proc.pid, None)
            stopped_by_shutdown = proc.pid in _stopped
            _stopped.discard(proc.pid)

    verdict = _parse_verdict(out)
    if verdict is None:
        if stopped_by_shutdown:
            raise GraphRebuildStopped(
                f"graph rebuild subprocess pid={proc.pid} stopped by node shutdown"
            )
        raise GraphRebuildSubprocessError(
            f"graph rebuild subprocess pid={proc.pid} rc={proc.returncode} "
            f"returned no verdict: {_stderr_tail(err)}"
        )
    status = str(verdict.get("status") or "")
    if status == "ok":
        logger.info(
            "graph rebuild subprocess pid=%s finished in %.1fs",
            proc.pid,
            time.monotonic() - started,
        )
        return dict(verdict.get("report") or {})
    if status == "deferred":
        raise WriteGateDeferred(str(verdict.get("reason") or "graph rebuild already running"))
    raise GraphRebuildSubprocessError(
        str(verdict.get("error") or f"graph rebuild subprocess failed rc={proc.returncode}")
        + (f" — {_stderr_tail(err)}" if err else "")
    )


def _signal_tree(proc: subprocess.Popen, *, hard: bool) -> None:
    """SIGTERM (or SIGKILL when ``hard``) the child's whole process group when it
    leads one, else the child alone.

    Signalling the child alone left its own children holding the pipes, so the
    drain after a kill waited for them instead of the child — hence the group,
    whenever the child has one of its own.
    """
    if getattr(proc, "_topos_own_group", False):
        try:
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            return
        except ProcessLookupError:
            return  # the whole group is already gone
        except OSError as exc:  # pragma: no cover - unexpected; fall back to the pid
            logger.debug("killpg(%s) failed (%s); signalling the child only", proc.pid, exc)
    try:
        proc.kill() if hard else proc.terminate()
    except ProcessLookupError:  # pragma: no cover - raced its own exit
        pass


def _drain_after_kill(proc: subprocess.Popen) -> None:
    """Collect the killed child without waiting forever on a pipe it no longer holds."""
    try:
        proc.communicate(timeout=_DRAIN_AFTER_KILL_S)
    except subprocess.TimeoutExpired:
        logger.warning(
            "graph rebuild subprocess pid=%s: pipes still open %.0fs after kill; not waiting",
            proc.pid,
            _DRAIN_AFTER_KILL_S,
        )


def _kill_groups(procs: Iterable[subprocess.Popen]) -> None:
    for proc in procs:
        _signal_tree(proc, hard=True)


def _stop_now(
    procs: List[subprocess.Popen], kill_after_s: Optional[float], *, mark: bool = True
) -> None:
    """Mark, SIGTERM, and (after ``kill_after_s``) SIGKILL each child's group.

    Lock-free on purpose: it runs inside a signal handler whose main thread may
    hold ``_live_lock``. A pid is marked only while its waiter still has it
    registered: a child that finished on its own meanwhile is already
    collected, and a mark left for it would outlive it — and could later
    misname a new child that reused the pid.
    """
    if not procs:
        return
    if mark:
        _stopped.update(p.pid for p in procs if p.pid in _live)
    for proc in procs:
        logger.info("stopping graph rebuild subprocess pid=%s: node is shutting down", proc.pid)
        _signal_tree(proc, hard=False)
    if kill_after_s is not None:
        # SIGKILL the group even when the child itself obeyed SIGTERM: a member
        # that ignores it keeps the pipes, and with them the waiter, open.
        killer = threading.Timer(kill_after_s, _kill_groups, args=(list(procs),))
        killer.daemon = True
        killer.name = "rebuild-child-kill"
        killer.start()


def signal_rebuild_children(kill_after_s: float = _KILL_AFTER_S) -> int:
    """Stop every rebuild child NOW, without waiting; close the spawn gate.

    Registered as a runtime_shutdown listener, so it runs the moment a SIGINT or
    SIGTERM arrives — inside the signal handler, on whatever the main thread was
    doing. It therefore takes no lock: ``list(dict.values())`` is one atomic
    step under the GIL.

    The signal is the only place early enough. uvicorn drains in-flight
    requests BEFORE lifespan shutdown, so a rebuild awaited by POST
    /entities/graph/rebuild held the node open with shutdown_event never
    reached; and a child in its own session no longer hears a terminal's Ctrl+C.
    """
    global _accepting
    _accepting = False
    procs = list(_live.values())
    _stop_now(procs, kill_after_s)
    return len(procs)


def stop_rebuild_children(grace_s: float = 2.0) -> int:
    """Stop every rebuild child and wait for it; close the spawn gate.

    For app shutdown (both ends of shutdown_event) and the tray's re-exec.
    SIGTERM first — the child installs no handler, so it dies at once and SQLite
    rolls back its open transaction — then SIGKILL to each whole group after
    ``grace_s``, whether or not the child itself is already gone. The thread
    waiting on each child gets :class:`GraphRebuildStopped` instead of holding
    the node's exit: a rebuild awaited through asyncio.to_thread otherwise held
    asyncio.run in shutdown_default_executor until the child finished on its own.

    The group kill needs a child that leads its own group (the app with its
    signal hooks installed). One left in the caller's group — tray mode, a CLI
    command — gets the child-only kill, so a grandchild that holds the pipes,
    ignores SIGTERM and outlives its parent would still hold the waiter.
    multiprocessing's resource_tracker is not one: it exits with its parent.
    """
    global _accepting
    with _live_lock:
        _accepting = False
        procs = list(_live.values())
        _stopped.update(p.pid for p in procs)
    _stop_now(procs, kill_after_s=None, mark=False)
    deadline = time.monotonic() + max(0.0, grace_s)
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    _kill_groups(procs)
    for proc in procs:
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            logger.warning("graph rebuild subprocess pid=%s survived SIGKILL", proc.pid)
    return len(procs)


def accepting() -> bool:
    """False from a shutdown's first stop until the next app startup reopens it."""
    return _accepting


def allow_rebuild_children() -> None:
    """Reopen the spawn gate a shutdown closed. App startup: a new run may rebuild."""
    global _accepting
    with _live_lock:
        _accepting = True
        # Marks for children already collected can only mislead from here on.
        _stopped.intersection_update(_live)


def _parse_verdict(stdout: Optional[str]) -> Optional[Dict[str, Any]]:
    """Last JSON object on stdout with a ``status`` key.

    Scanned from the end so stray library prints above the verdict line can't
    break parsing.
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "status" in obj:
            return obj
    return None


def _stderr_tail(stderr: Optional[str], lines: int = 8) -> str:
    tail = [l for l in (stderr or "").strip().splitlines() if l.strip()][-lines:]
    return " | ".join(tail) if tail else "(no stderr)"


# ---------------------------------------------------------------------------
# Child side
# ---------------------------------------------------------------------------


def _exit_orphaned() -> None:
    """Die now, taking this child's own process group with it when it leads one."""
    try:
        # Only when we lead our group: without start_new_session the group is
        # the NODE's, and killpg would take the node down.
        if hasattr(os, "killpg") and os.getpgid(0) == os.getpid():
            os.killpg(os.getpid(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(1)


def _watch_parent(parent_pid: int, poll_s: float = _PARENT_POLL_S) -> None:
    """Exit when the node that started this rebuild is gone.

    A parent that dies — SIGKILL included, which no shutdown hook sees — gets
    its children reparented, so ``getppid()`` stops matching. Polled rather
    than signalled: macOS has no PR_SET_PDEATHSIG.
    """
    if os.getppid() != parent_pid:
        _exit_orphaned()

    def _run() -> None:
        while True:
            time.sleep(poll_s)
            if os.getppid() != parent_pid:
                _exit_orphaned()

    threading.Thread(target=_run, name="rebuild-parent-watch", daemon=True).start()


def _parent_pid_from_env() -> Optional[int]:
    try:
        pid = int(os.environ.get(_PARENT_PID_ENV, ""))
    except ValueError:
        return None
    return pid if pid > 1 else None


def _acquire_rebuild_lock(db_path: str):
    """Exclusive advisory lock beside the database; None when already held.

    Returns an open file object holding the flock (kept for the child's
    lifetime; the OS releases it on any exit). A filesystem that can't take
    the lock degrades to running unlocked — availability over exclusion.
    """
    lock_path = db_path + ".rebuild.lock"
    try:
        import fcntl

        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle
    except ImportError:  # pragma: no cover - non-POSIX
        return open(lock_path, "a+")
    except OSError as exc:
        logger.warning("rebuild lock unavailable (%s); continuing unlocked", exc)
        return object()  # truthy sentinel: proceed without a real lock


def _child_rebuild(db_path: str) -> Dict[str, Any]:
    if not os.path.isfile(db_path):
        return {"status": "error", "error": f"database not found: {db_path}"}

    lock = _acquire_rebuild_lock(db_path)
    if lock is None:
        return {"status": "deferred", "reason": "another graph rebuild holds the lock"}

    conn: Optional[sqlite3.Connection] = None
    try:
        from ...storage.db.connection_tuning import tune_connection
        from .maintenance import rebuild_entity_graph

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tune_connection(conn)  # WAL + busy_timeout: how we wait on the node's writers
        report = rebuild_entity_graph(conn)
        return {"status": "ok", "report": report}
    except Exception as exc:  # noqa: BLE001 — verdict, not traceback, crosses the pipe
        logger.exception("graph rebuild failed")
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if hasattr(lock, "close"):
            lock.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parent_pid = _parent_pid_from_env()
    if parent_pid is not None:
        _watch_parent(parent_pid)
    if len(args) != 1:
        print(json.dumps({"status": "error", "error": "usage: rebuild_subprocess <db_path>"}))
        return 2
    verdict = _child_rebuild(args[0])
    print(json.dumps(verdict), flush=True)
    return 0 if verdict["status"] in ("ok", "deferred") else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
