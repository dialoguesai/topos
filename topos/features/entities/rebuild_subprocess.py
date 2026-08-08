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

Cost accepted: the child cold-loads the embedding model each run (the node's
in-process cache doesn't carry over). Rebuilds are debounced to a few per hour
at most; a few seconds of model load per run buys a loop that never goes dark.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from ...storage.db.write_gate import WriteGateDeferred

logger = logging.getLogger("topos.features.entities.rebuild_subprocess")

_DEFAULT_TIMEOUT_S = 1800.0


class GraphRebuildSubprocessError(RuntimeError):
    """The rebuild child crashed, hung, or returned no verdict."""


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


def rebuild_in_subprocess(db_path: str, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Spawn the rebuild child on ``db_path`` and return its report."""
    timeout = _timeout_s() if timeout_s is None else timeout_s
    db_path = os.path.abspath(db_path)
    env = dict(os.environ)
    # Pin any get_db_connection() fallback inside the child to the same file,
    # and keep the child from arming its own debounced refresher timers.
    env["TOPOS_DATABASE_PATH"] = db_path
    env["TOPOS_GRAPH_REFRESH"] = "off"
    cmd = [sys.executable, "-m", "topos.features.entities.rebuild_subprocess", db_path]

    # ``-m`` prepends the child's cwd to sys.path, so a node whose cwd holds a
    # DIFFERENT topos checkout would silently run that version's rebuild.
    # Spawning from this package's own parent pins the child to the exact code
    # the parent is executing.
    import topos as _topos_pkg

    code_root = os.path.dirname(os.path.dirname(os.path.abspath(_topos_pkg.__file__)))

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=code_root
    )
    logger.info("graph rebuild subprocess started pid=%s db=%s", proc.pid, db_path)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise GraphRebuildSubprocessError(
            f"graph rebuild subprocess pid={proc.pid} exceeded {timeout:.0f}s and was killed"
        ) from None

    verdict = _parse_verdict(out)
    if verdict is None:
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
    if len(args) != 1:
        print(json.dumps({"status": "error", "error": "usage: rebuild_subprocess <db_path>"}))
        return 2
    verdict = _child_rebuild(args[0])
    print(json.dumps(verdict), flush=True)
    return 0 if verdict["status"] in ("ok", "deferred") else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())
