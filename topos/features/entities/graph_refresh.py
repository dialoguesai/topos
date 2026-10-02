"""Debounced post-enrichment entity-graph refresh.

The 1.2.0 graph layers (materialized facts/goals/places/conversations,
provenance roles on edges, Louvain neighborhoods) are derived by
``rebuild_entity_graph`` — which, until this module, had exactly one caller: a
manual HTTP endpoint no fresh user knows exists. Enrichment completion now
marks the graph dirty; after a quiet debounce window a single background
rebuild runs, so the graph stays derived without anyone babysitting an
endpoint. The permissions lane's derivation marks it too when it stores goals
or facts (``record_graph_dirty`` + ``schedule_graph_refresh``), so their nodes
and ``pursues`` edges do not wait for the next enrichment run.

Design constraints (all learned live):
  * thread-timer based — enrichment completes in both async (FastAPI loop) and
    worker-thread contexts, so no event-loop dependency;
  * single-flight — one rebuild at a time; a mark landing mid-rebuild schedules
    exactly one follow-up (enrichment walks sources serially; without this the
    walk would queue N rebuilds);
  * failures disarm nothing — a failed rebuild logs and the next mark tries
    again;
  * kill-switch TOPOS_GRAPH_REFRESH=off, window TOPOS_GRAPH_REFRESH_DEBOUNCE_S
    (default 90s: long enough to coalesce a multi-source enrichment walk,
    short enough that a fresh install sees its graph within minutes of first
    sync);
  * a mark is a reason to look, not a reason to rebuild (1.4.4). Every path
    into a rebuild -- the debounced timer, the pipeline's inline fill, the
    startup reconcile -- goes through ``rebuild_if_inputs_changed``, which
    rebuilds only when the fingerprint of everything the rebuild reads
    (``graph_inputs``) differs from the one the last successful rebuild
    stored, or that rebuild is older than TOPOS_GRAPH_REFRESH_MAX_AGE_S
    (default 6h; 0 turns the age limit off). On the owner's node the
    pipeline marked the graph for every browser-visit batch and paid two
    rebuilds of ~130s each per batch, all reporting the same counts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from ...storage.db.write_gate import WriteGateDeferred

logger = logging.getLogger("topos.features.entities.graph_refresh")

_DEFAULT_DEBOUNCE_S = 90.0
_DEFAULT_MAX_AGE_S = 6 * 3600.0
_DEFAULT_RETRY_S = 30 * 60.0
# Suffix on a stored fingerprint whose rebuild reported a part incomplete.
_INCOMPLETE = "|incomplete"


def _debounce_seconds() -> float:
    try:
        return max(0.05, float(os.environ.get("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", _DEFAULT_DEBOUNCE_S)))
    except (TypeError, ValueError):
        return _DEFAULT_DEBOUNCE_S


def _max_age_seconds() -> float:
    """How old the last rebuild may be before unchanged inputs rebuild anyway; 0 = no limit."""
    try:
        return max(0.0, float(os.environ.get("TOPOS_GRAPH_REFRESH_MAX_AGE_S", _DEFAULT_MAX_AGE_S)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE_S


def _retry_seconds() -> float:
    """The same limit after a rebuild that reported a part incomplete; 0 = no limit."""
    try:
        return max(0.0, float(os.environ.get("TOPOS_GRAPH_REFRESH_RETRY_S", _DEFAULT_RETRY_S)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_S


def _enabled() -> bool:
    return os.environ.get("TOPOS_GRAPH_REFRESH", "on").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _default_rebuild() -> Dict[str, Any]:
    from ...core.state import close_thread_db_connection, get_db_connection
    from ...enrichment.pipeline_activity import is_derivation_in_flight
    from ...storage.db.write_gate import WriteGateDeferred

    conn = get_db_connection()
    if conn is None:
        logger.debug("graph refresh skipped: no database connection")
        return {"skipped": "no database"}
    try:
        # No outer gate hold: the rebuild gates its own write phases (M2.2) —
        # the gate is reentrant, so wrapping it here would silently reinstate
        # the whole-rebuild exclusive hold (120s observed 2026-08-07) that
        # starved every other writer. The cooperative contract survives as a
        # pre-check: never START against an in-flight derivation batch
        # (WriteGateDeferred re-arms the debounce in _fire). A batch that
        # starts mid-rebuild now waits at most one short phase hold, and
        # enrichment completion re-marks the graph dirty, so a premature
        # result is rebuilt rather than defended against here.
        #
        # run_graph_rebuild sends the compute to a SUBPROCESS when the database
        # is file-backed: even fully gate-disciplined, the in-process rebuild's
        # CPU work (goal embeddings, role map, Louvain) held the GIL so hard
        # the event loop served nothing for ~103s (2026-08-08, zero WRITE_GATE
        # warnings). This thread then just waits on the child, GIL-free.
        if is_derivation_in_flight():
            raise WriteGateDeferred("derivation batch in flight")
        return rebuild_if_inputs_changed(conn)
    finally:
        # This runs on a short-lived Timer thread, which now gets its own
        # connection; without this each refresh would leak one.
        close_thread_db_connection()


def rebuild_if_inputs_changed(
    conn, *, rebuild: Optional[Callable[[Any], Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Rebuild the graph unless nothing it reads has changed since the last rebuild.

    The order is the point:

    1. Close dangling derived objects first, as every rebuild did. That sweep
       covers objects the graph does not read (on the owner's node, five
       re-derived ``message_topics`` per pipeline batch), so it runs on every
       trigger, keeping its old cadence whether or not the graph rebuilds; a
       fact it closes changes the fingerprint below, and the graph then sheds
       that fact's edge.
    2. Read the dirty generation BEFORE the inputs. A skip or a rebuild covers
       the marks up to here and no further: a mark that lands while the
       fingerprint is read, or while the rebuild runs, leaves the state dirty,
       and the next trigger looks again.
    3. Fingerprint the inputs. Equal to the stored one, and the last rebuild
       younger than the age limit: absorb the marks, write nothing else.
    4. Otherwise rebuild, then store this fingerprint -- the one taken BEFORE
       the rebuild read anything, so a change that lands mid-rebuild still
       differs next time. A rebuild that reports a part ``incomplete`` (a lane
       that raised, centrality that failed, goals clustered by tokens because
       the embedder was unavailable) is stored as such, and unchanged inputs
       rebuild again once it is older than TOPOS_GRAPH_REFRESH_RETRY_S
       (default 30 min) rather than the 6h age limit: a transient failure is
       repaired within the half hour, and a node whose embedder never loads
       rebuilds twice an hour, not on every mark.

    No outer gate hold, for the same reason as ``_default_rebuild``.
    """
    from ...storage.db.write_gate import with_db_write
    from .graph_inputs import graph_input_fingerprint

    closed = _close_dangling(conn)
    generation = _dirty_generation(conn)
    fingerprint = graph_input_fingerprint(conn)
    stored, last_run_at = _stored_fingerprint(conn)
    stored_incomplete = bool(stored and stored.endswith(_INCOMPLETE))
    if stored_incomplete:
        stored = stored[: -len(_INCOMPLETE)]
    limit = _retry_seconds() if stored_incomplete else _max_age_seconds()
    if fingerprint is not None and fingerprint == stored and not _overdue(last_run_at, limit):
        _absorb_generation(conn, generation)
        logger.info("graph refresh skipped: nothing the graph reads has changed since the last rebuild")
        return {"skipped": "inputs unchanged", "dangling_closed": closed}
    if rebuild is None:
        from .rebuild_subprocess import run_graph_rebuild

        rebuild = run_graph_rebuild
    from ...enrichment.pipeline_activity import is_derivation_in_flight

    if is_derivation_in_flight():
        # Checked again: a batch may have started while the inputs were read.
        raise WriteGateDeferred("derivation batch in flight")
    logger.info(
        "graph refresh: rebuilding (%s)",
        "no stored fingerprint" if not stored
        else "fingerprint unavailable" if fingerprint is None
        else "inputs changed" if fingerprint != stored
        else "last rebuild incomplete, retrying" if stored_incomplete
        else "last rebuild older than the age limit",
    )
    report = rebuild(conn)
    complete = isinstance(report, dict) and not report.get("incomplete")
    stamp = None if fingerprint is None else fingerprint if complete else fingerprint + _INCOMPLETE
    try:
        with with_db_write():
            _mark_materialized(conn, generation=generation, fingerprint=stamp)
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph materialization stamp failed: %s", exc)
    logger.info("graph refresh: %s", report)
    return {"ran": True, "report": report}


def _close_dangling(conn) -> int:
    """``close_dangling_facts``: reads ungated, writes in its own gated batch."""
    try:
        from ..lifecycle.derived_scrub import close_dangling_facts

        return int(close_dangling_facts(conn) or 0)
    except Exception as exc:  # noqa: BLE001 -- the rebuild runs it again and reports
        logger.debug("dangling-object sweep before the graph check failed: %s", exc)
        return 0


def _dirty_generation(conn) -> Optional[int]:
    try:
        row = conn.execute(
            "SELECT dirty_generation FROM graph_materialization_state WHERE id=1"
        ).fetchone()
    except Exception:  # noqa: BLE001 -- no state: nothing to absorb or stamp
        return None
    return int(row[0]) if row and row[0] is not None else None


def _stored_fingerprint(conn) -> Tuple[Optional[str], Optional[str]]:
    """(input_fingerprint, last_run_at) of the last successful rebuild; Nones when unknown."""
    try:
        row = conn.execute(
            "SELECT input_fingerprint, last_run_at FROM graph_materialization_state WHERE id=1"
        ).fetchone()
    except Exception:  # noqa: BLE001 -- no column yet (first run on this node): rebuild
        return None, None
    if not row:
        return None, None
    return (str(row[0]) if row[0] else None), (str(row[1]) if row[1] else None)


def _overdue(last_run_at: Optional[str], limit: float) -> bool:
    if limit <= 0:
        return False
    if not last_run_at:
        return True
    try:
        then = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() > limit


def _absorb_generation(conn, generation: Optional[int]) -> None:
    """Mark the graph current up to ``generation``: those marks changed nothing it reads."""
    if generation is None:
        return
    from ...storage.db.write_gate import commit_connection, with_db_write

    try:
        with with_db_write():
            conn.execute(
                "UPDATE graph_materialization_state SET materialized_generation=? "
                "WHERE id=1 AND materialized_generation < ?",
                (generation, generation),
            )
            commit_connection(conn)
    except Exception as exc:  # noqa: BLE001 -- the next trigger looks again
        logger.debug("graph generation absorb failed: %s", exc)


class _Refresher:
    def __init__(self, rebuild_fn: Optional[Callable[[], Any]] = None):
        self._rebuild_fn = rebuild_fn or _default_rebuild
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._dirty_during_run = False
        self._dirty = False
        self._last_run_at: Optional[str] = None
        self._last_error: Optional[str] = None

    def mark(self) -> None:
        if not _enabled():
            return
        with self._lock:
            self._dirty = True
            if self._running:
                # Coalesce into one follow-up after the in-flight rebuild.
                self._dirty_during_run = True
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_debounce_seconds(), self._fire)
            self._timer.daemon = True
            # Named, because this thread does database work minutes after the
            # call that armed it and every diagnostic that catches it in the act
            # prints only a thread name. Under the default `Thread-N` a refusal
            # from the test suite's live-db guard reads as an anonymous thread
            # inside whichever test was unlucky enough to be running (2026-08-20).
            self._timer.name = "topos-graph-refresh-debounce"
            self._timer.start()

    def _fire(self) -> None:
        # A full rebuild during a derivation batch is both contended and
        # premature: the batch is still writing the entities this would index,
        # and even with the M2.2 phase-chunked gate holds the rebuild's write
        # phases contend with every batch commit. Re-arm instead and rebuild
        # once the batch is done.
        try:
            from ...enrichment.pipeline_activity import is_derivation_in_flight

            if is_derivation_in_flight():
                with self._lock:
                    self._timer = None
                    self._dirty = True
                logger.debug("graph refresh deferred: derivation batch in flight")
                self.mark()
                return
        except Exception:  # noqa: BLE001 — coordination is best-effort
            pass

        with self._lock:
            self._timer = None
            if self._running:
                self._dirty_during_run = True
                return
            self._running = True
            self._dirty = False
        logger.info("graph refresh: started")
        deferred = False
        try:
            self._rebuild_fn()
            self._last_error = None
        except WriteGateDeferred as exc:
            # Not a failure: a derivation batch owns (or is about to take) the
            # write gate. Step aside and retry after the next debounce window.
            deferred = True
            logger.info("graph refresh deferred: %s", exc)
        except Exception as exc:  # noqa: BLE001 — refresh must never die
            self._last_error = str(exc)
            logger.warning("graph refresh failed: %s", exc)
        finally:
            if not deferred:
                self._last_run_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._running = False
                rerun = self._dirty_during_run or deferred
                self._dirty_during_run = False
            if rerun:
                self.mark()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": _enabled(),
                "dirty": self._dirty or self._dirty_during_run,
                "running": self._running,
                "last_run_at": self._last_run_at,
                "last_error": self._last_error,
                "debounce_s": _debounce_seconds(),
            }

    def shutdown(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


_refresher = _Refresher()


_GC_LOCK = threading.Lock()
_GC_TIMER: Optional[threading.Timer] = None
_GC_LAST: Dict[str, Any] = {}


def _gc_enabled() -> bool:
    return os.getenv("TOPOS_GC_SWEEP", "on").strip().lower() not in {"off", "0", "false"}


def _run_gc_now() -> None:
    """The derived-drift sweep, on its own debounce.

    Separate timer from the graph rebuild deliberately. The rebuild is the
    common case and must stay quick to arm; the sweep touches more tables and
    is worth coalescing over a longer window, and a failure in one must not
    disarm the other.
    """
    from ...core import state as _state
    from ..lifecycle.gc import run_gc

    conn = _state.get_db_connection()
    if conn is None:
        return
    try:
        _GC_LAST.update(run_gc(conn))
        _GC_LAST["ran_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001 — maintenance never breaks the caller
        logger.warning("gc sweep failed: %s", exc)
        _GC_LAST["error"] = str(exc)


def mark_gc_due() -> None:
    """Enrichment completed — schedule the debounced derived-drift sweep.

    `run_gc` previously had no caller anywhere: the maintenance pass was itself
    the stored-but-never-applied pattern this workstream keeps finding. The
    corrections inside it are ones new data recreates — a sync can mint another
    timeline twin or another place-name stub cluster — so they have to re-run
    rather than be fixed once by a migration.
    """
    global _GC_TIMER
    if not _gc_enabled():
        return
    delay = float(os.getenv("TOPOS_GC_DEBOUNCE_S", "300") or 300)
    with _GC_LOCK:
        if _GC_TIMER is not None:
            _GC_TIMER.cancel()
        _GC_TIMER = threading.Timer(delay, _run_gc_now)
        _GC_TIMER.daemon = True
        _GC_TIMER.name = "topos-gc-sweep-debounce"
        _GC_TIMER.start()


def gc_status() -> Dict[str, Any]:
    """What the last sweep did, so an unrun sweep is visible rather than silent."""
    with _GC_LOCK:
        return {"enabled": _gc_enabled(), "pending": _GC_TIMER is not None, **_GC_LAST}


def refresh_now_if_dirty() -> Dict[str, Any]:
    """Rebuild the graph inline, right now, if it is dirty and nothing is deriving.

    Written for one caller: the post-canonical pipeline, at the point between
    canonical enrichment and signal derivation. The debounced mark cannot do
    this job there -- its timer fires ~90s later, by which time signal
    derivation has raised the in-flight counter and _fire re-arms the timer
    instead of rebuilding (DEBUG-logged). It keeps re-arming until derivation
    ends, so the mid-import refresh collapses into the end-of-pipeline one. On
    a file import that phase runs for hours, so "the graph fills before the
    long phase" never happened. Measured on a 20-stage import: zero mid-import
    rebuilds.

    Costs a rebuild (~7-9 minutes on a 17k-edge node) inside the import, which
    is the point. Skipped when the graph is not dirty, so a re-import that
    changed nothing pays nothing.

    "Dirty" is not "changed": the pipeline marks the graph just before calling
    this, for every batch, so the check above hardly ever skipped anything; on
    the owner's node a single browser visit cost a full rebuild here (1.4.3).
    The rebuild function now skips when nothing the graph reads has changed
    (``rebuild_if_inputs_changed``), and so does this.
    """
    from ...core.state import get_db_connection
    from ...enrichment.pipeline_activity import is_derivation_in_flight

    if not _enabled():
        return {"skipped": "disabled"}
    if is_derivation_in_flight():
        return {"skipped": "derivation in flight"}
    conn = get_db_connection()
    if conn is None:
        return {"skipped": "no database"}
    try:
        row = conn.execute(
            "SELECT dirty_generation, materialized_generation FROM graph_materialization_state WHERE id=1"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"state unreadable: {exc}"}
    if not row or int(row[0]) <= int(row[1]):
        return {"skipped": "not dirty"}
    logger.info("graph refresh: inline, before signal derivation")
    try:
        result = _refresher._rebuild_fn()
        if isinstance(result, dict) and result.get("skipped"):
            return {"skipped": str(result["skipped"])}
        return {"ran": True}
    except WriteGateDeferred as exc:
        _refresher.mark()
        return {"skipped": f"deferred: {exc}"}
    finally:
        # This runs on an asyncio.to_thread worker. Pooled threads are reused;
        # the rebuild route closes its thread-local connection for the same
        # reason, or every call leaks one.
        try:
            from ...core.state import close_thread_db_connection

            close_thread_db_connection()
        except Exception:  # noqa: BLE001 — cleanup must not mask the result
            pass


def mark_graph_dirty() -> None:
    """Enrichment completed for a source — schedule a debounced graph rebuild."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _persist_dirty_generation_this_thread()
    else:
        # The generation bump takes the write gate — a blocking OS lock. Taken
        # here, on the event-loop thread, it stalls every coroutine behind
        # whatever writer currently holds it (this exact call site was one of
        # the acquisitions in the 2026-08-07 loop freeze). Persist from the
        # default executor instead; that thread fetches its own connection.
        loop.run_in_executor(None, _persist_dirty_generation_this_thread)
    _refresher.mark()


def record_graph_dirty(conn) -> bool:
    """``mark_graph_dirty``'s persisted half, for a writer on a connection of its own.

    A writer of rows the graph derives from that writes the node's database on its
    own connection, inside its own write transaction, rather than on this thread's
    ``get_db_connection()`` (the permissions lane's goal and fact writes,
    ``permitted_derivation``), bumps the dirty generation on that connection, in
    that transaction: the mark commits with its rows, and a node that stops before
    the debounce fires still rebuilds at startup (``reconcile_graph_on_startup``).
    The caller holds the write gate and commits, then calls
    ``schedule_graph_refresh``. Returns False when the node has no
    ``graph_materialization_state`` row (nothing is recorded; the debounce still
    runs). Raises what the database raises.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_materialization_state'"
    ).fetchone():
        return False
    return conn.execute(
        "UPDATE graph_materialization_state SET dirty_generation = dirty_generation + 1 WHERE id = 1"
    ).rowcount == 1


def schedule_graph_refresh() -> None:
    """``mark_graph_dirty``'s second half: arm the debounced rebuild, with the same
    kill switch (``TOPOS_GRAPH_REFRESH``), coalescing and single flight. For a
    writer that recorded the mark itself (``record_graph_dirty``), after its commit."""
    _refresher.mark()


def _persist_dirty_generation_this_thread() -> None:
    """Bump the persisted dirty generation on THIS thread's own connection."""
    try:
        from ...core.state import get_db_connection

        conn = get_db_connection()
        if conn is not None:
            _persist_dirty_generation(conn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph dirty persistence skipped: %s", exc)


def _writing_thread_owns(conn) -> bool:
    """True when this thread may write on ``conn`` without racing another.

    Safe when the connection is this thread's own (``core.state`` hands every
    thread a private one for a file-backed database), or when we ARE the owner
    thread that holds the module-global handle.

    Neither holds for a SHARED connection, and shared is what
    ``get_db_connection`` returns whenever the database is in-memory — a
    per-thread copy would be empty, so ``core.state`` deliberately hands out
    the owner's — or whenever a test injected a handle of its own. Writing
    then races the thread that is already using it: a ``sqlite3.Connection``
    carries exactly ONE transaction state, and the write gate serializes
    writers, not readers. That corrupts the transaction state and, on CPython
    3.12, segfaulted the CI test lane from this very call site.
    """
    import threading as _threading

    from ...core import state as core_state

    if conn is getattr(core_state._thread_state, "conn", None):
        return True
    return _threading.get_ident() == core_state._conn_owner_thread


def _persist_dirty_generation(conn) -> None:
    from ...storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
    from ...storage.db.write_gate import commit_connection, with_db_write

    if not _writing_thread_owns(conn):
        # Shared handle: skip rather than corrupt it. Only reachable off the
        # owner thread with an in-memory or injected database, i.e. tests --
        # a file-backed node always gets a private connection here.
        logger.debug("graph dirty persistence skipped: connection is shared across threads")
        return

    apply_pipeline_jobs_v1_up(conn)
    with with_db_write():
        conn.execute(
            """
            UPDATE graph_materialization_state
            SET dirty_generation = dirty_generation + 1
            WHERE id = 1
            """
        )
        commit_connection(conn)


def reconcile_graph_on_startup(conn) -> None:
    """Rebuild immediately when persisted dirty generation trails materialized."""
    from ...storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    apply_pipeline_jobs_v1_up(conn)
    row = conn.execute(
        "SELECT dirty_generation, materialized_generation FROM graph_materialization_state WHERE id=1"
    ).fetchone()
    if not row:
        return
    dirty, materialized = int(row[0]), int(row[1])
    if dirty > materialized:
        try:
            _refresher._rebuild_fn()
        except WriteGateDeferred as exc:
            # A derivation is already running at startup; catch up debounced.
            logger.info("startup graph reconcile deferred: %s", exc)
            _refresher.mark()


def _mark_materialized(
    conn, *, generation: Optional[int] = None, fingerprint: Optional[str] = None
) -> None:
    """Stamp a successful rebuild: the marks it covers, when, and what it read.

    ``generation`` is the dirty generation read before the rebuild started;
    marks after it stay outstanding. ``None`` keeps the old stamp-everything
    behaviour for a caller that read none. ``fingerprint`` is what the rebuild
    read (``graph_inputs``); ``None`` stores none, so the next trigger rebuilds.
    """
    from ...storage.db.write_gate import commit_connection

    now = datetime.now(timezone.utc).isoformat()
    has_column = _ensure_fingerprint_column(conn)
    covered = "dirty_generation" if generation is None else "MAX(materialized_generation, ?)"
    params: list = [] if generation is None else [int(generation)]
    if has_column:
        conn.execute(
            f"""
            UPDATE graph_materialization_state
            SET materialized_generation = {covered},
                last_run_at = ?,
                last_error = NULL,
                input_fingerprint = ?
            WHERE id = 1
            """,
            (*params, now, fingerprint),
        )
    else:
        conn.execute(
            f"""
            UPDATE graph_materialization_state
            SET materialized_generation = {covered},
                last_run_at = ?,
                last_error = NULL
            WHERE id = 1
            """,
            (*params, now),
        )
    commit_connection(conn)


def _ensure_fingerprint_column(conn) -> bool:
    """Add ``graph_materialization_state.input_fingerprint`` in place when it is missing.

    In place rather than as a numbered migration on purpose: a nullable column
    nothing else reads changes no ``user_version``, so a node that goes back to
    1.4.3 opens this database without a downgrade fence (1.4.3 names its
    columns and never sees this one). A fresh install and an upgrading node
    both get it here, at their first stamped rebuild. False when there is no
    state table (nothing to stamp) or the column cannot be added (the gate then
    never skips: the old behaviour).
    """
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(graph_materialization_state)")}
    except Exception:  # noqa: BLE001
        return False
    if not cols:
        return False
    if "input_fingerprint" in cols:
        return True
    try:
        conn.execute("ALTER TABLE graph_materialization_state ADD COLUMN input_fingerprint TEXT")
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph input fingerprint column not added: %s", exc)
        return False
    return True


def status() -> Dict[str, Any]:
    return _refresher.status()


def reset_for_tests(rebuild_fn: Optional[Callable[[], None]] = None) -> None:
    global _refresher
    _refresher.shutdown()
    _refresher = _Refresher(rebuild_fn=rebuild_fn)
