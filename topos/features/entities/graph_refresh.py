"""Debounced post-enrichment entity-graph refresh.

The 1.2.0 graph layers (materialized facts/goals/places/conversations,
provenance roles on edges, Louvain neighborhoods) are derived by
``rebuild_entity_graph`` — which, until this module, had exactly one caller: a
manual HTTP endpoint no fresh user knows exists. Enrichment completion now
marks the graph dirty; after a quiet debounce window a single background
rebuild runs, so the graph stays derived without anyone babysitting an
endpoint.

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
    sync).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ...storage.db.write_gate import WriteGateDeferred

logger = logging.getLogger("topos.features.entities.graph_refresh")

_DEFAULT_DEBOUNCE_S = 90.0


def _debounce_seconds() -> float:
    try:
        return max(0.05, float(os.environ.get("TOPOS_GRAPH_REFRESH_DEBOUNCE_S", _DEFAULT_DEBOUNCE_S)))
    except (TypeError, ValueError):
        return _DEFAULT_DEBOUNCE_S


def _enabled() -> bool:
    return os.environ.get("TOPOS_GRAPH_REFRESH", "on").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _default_rebuild() -> None:
    from ...core.state import close_thread_db_connection, get_db_connection
    from ...storage.db.write_gate import with_db_write
    from .maintenance import rebuild_entity_graph

    conn = get_db_connection()
    if conn is None:
        logger.debug("graph refresh skipped: no database connection")
        return
    try:
        # No outer with_db_write: the rebuild gates its own write phases
        # (M2.2). The gate is reentrant, so wrapping it here would silently
        # reinstate the whole-rebuild exclusive hold (120s observed
        # 2026-08-07) that starved every other writer.
        report = rebuild_entity_graph(conn)
        try:
            with with_db_write():
                _mark_materialized(conn)
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph materialization stamp failed: %s", exc)
        logger.info("graph refresh: %s", report)
    finally:
        # This runs on a short-lived Timer thread, which now gets its own
        # connection; without this each refresh would leak one.
        close_thread_db_connection()


class _Refresher:
    def __init__(self, rebuild_fn: Optional[Callable[[], None]] = None):
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


def _persist_dirty_generation_this_thread() -> None:
    """Bump the persisted dirty generation on THIS thread's own connection."""
    try:
        from ...core.state import get_db_connection

        conn = get_db_connection()
        if conn is not None:
            _persist_dirty_generation(conn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph dirty persistence skipped: %s", exc)


def _persist_dirty_generation(conn) -> None:
    from ...storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
    from ...storage.db.write_gate import commit_connection, with_db_write

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


def _mark_materialized(conn) -> None:
    from ...storage.db.write_gate import commit_connection

    conn.execute(
        """
        UPDATE graph_materialization_state
        SET materialized_generation = dirty_generation,
            last_run_at = ?,
            last_error = NULL
        WHERE id = 1
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    commit_connection(conn)


def status() -> Dict[str, Any]:
    return _refresher.status()


def reset_for_tests(rebuild_fn: Optional[Callable[[], None]] = None) -> None:
    global _refresher
    _refresher.shutdown()
    _refresher = _Refresher(rebuild_fn=rebuild_fn)
