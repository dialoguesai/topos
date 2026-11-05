"""Query retrieve must leave the event loop on a file-backed node.

Live 2026-09-08: ``retrieve()`` (MiniLM + write-gate) ran inside the uvicorn
async handler, ``/healthcheck`` stopped answering, and the macOS tray went red.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from topos.query import pipeline as query_pipeline


@pytest.mark.asyncio
async def test_offload_stays_on_loop_without_file_backed_db(monkeypatch):
    monkeypatch.setattr(query_pipeline, "_query_work_stays_on_loop", lambda _adapters=None: True)
    seen = []

    def work(value: int) -> int:
        seen.append(asyncio.get_running_loop())
        return value + 1

    assert await query_pipeline._offload_query_work(work, 3) == 4
    assert seen  # ran in this coroutine, not a worker


@pytest.mark.asyncio
async def test_offload_hops_to_thread_on_file_backed_node(monkeypatch):
    monkeypatch.setattr(query_pipeline, "_query_work_stays_on_loop", lambda _adapters=None: False)
    loop = asyncio.get_running_loop()
    ran_on = {"loop": False}

    def work() -> str:
        try:
            asyncio.get_running_loop()
            ran_on["loop"] = True
        except RuntimeError:
            ran_on["loop"] = False
        return "ok"

    assert await query_pipeline._offload_query_work(work) == "ok"
    assert ran_on["loop"] is False
    assert loop.is_running()


def test_retrieve_call_sites_still_use_offload_helper():
    """Sep 8 tray-red class: retrieve() ran on the uvicorn loop. Pin the hop."""
    from pathlib import Path

    src = Path(query_pipeline.__file__).read_text()
    assert "self._retrieve_on_calling_thread" in src
    assert src.count("_offload_query_work") >= 4


# --- the worker uses its OWN connection, not the loop's -----------------------


def _owner_connection_on_this_thread(tmp_path, monkeypatch):
    """A file-backed runtime database whose owner thread is the caller's.

    Same shape as the live node: ``handle_query`` binds the orchestrator to the
    loop thread's handle and ``asyncio.to_thread`` workers resolve their own.
    """
    from topos.core import state

    db = tmp_path / "engine.db"
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)
    monkeypatch.setattr(state, "_thread_state", threading.local(), raising=False)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db, raising=False)
    owner = state.get_db_connection()
    if owner is None:
        pytest.skip("database could not be opened in this environment")
    return owner


@pytest.mark.asyncio
async def test_offloaded_session_store_runs_on_the_workers_own_connection(tmp_path, monkeypatch):
    """1.5.43 stress, 2026-11-05: two session ``put`` calls failed with SQLite's
    "another row available" — the loop's Connection written from a pool thread
    while the loop read on it. The day before, the same sharing left a failed
    artifact INSERT holding the write lock for 11.5 minutes.

    ``_thread_session_store`` asked ``_query_work_stays_on_loop``, which starts
    with ``asyncio.get_running_loop()``; a worker has none, so from the worker
    the answer was always "stay", and the store came back bound to the loop's
    handle. The worker must resolve its own.
    """
    from topos.core import state
    from topos.query.runtime import get_query_orchestrator

    owner = _owner_connection_on_this_thread(tmp_path, monkeypatch)
    orchestrator = get_query_orchestrator(conn=owner)
    # The live-node shape: this orchestrator's handle IS the runtime connection.
    assert query_pipeline._query_work_stays_on_loop(orchestrator._adapters) is False

    def resolve_on_worker():
        store = orchestrator._thread_session_store()
        own = state.get_db_connection()
        return {
            "thread": threading.get_ident(),
            "store_on_owner": store._conn is owner,
            "store_on_own": store._conn is own,
            "facts_conn_on_owner": orchestrator._worker_db_connection() is owner,
        }

    seen = await query_pipeline._offload_query_work(resolve_on_worker, adapters=orchestrator._adapters)
    assert seen["thread"] != threading.get_ident(), "the work must have left the loop thread"
    assert seen["store_on_owner"] is False, "a pool thread wrote through the loop's connection"
    assert seen["store_on_own"] is True
    assert seen["facts_conn_on_owner"] is False
    # Back on the loop, the marker is gone and the bound store is the right one.
    assert query_pipeline._on_offload_worker.get() is False
    assert orchestrator._thread_session_store() is orchestrator._session_store()


@pytest.mark.asyncio
async def test_offloaded_retrieve_runs_on_the_workers_own_connection(tmp_path, monkeypatch):
    """Same defect, retrieval side: ``_retrieve_on_calling_thread`` built its
    adapter on the loop's handle from the worker."""
    from topos.query.runtime import get_query_orchestrator

    owner = _owner_connection_on_this_thread(tmp_path, monkeypatch)
    orchestrator = get_query_orchestrator(conn=owner)
    seen = {}

    class _Adapter:
        def __init__(self, adapters):
            seen["conn"] = adapters.query_session._conn

        def retrieve(self, request):
            return request

    monkeypatch.setattr(query_pipeline, "DefaultSignalRetrievalAdapter", _Adapter)
    result = await query_pipeline._offload_query_work(
        orchestrator._retrieve_on_calling_thread, "request", adapters=orchestrator._adapters
    )
    assert result == "request"
    assert seen["conn"] is not owner


def test_worker_bundle_is_built_once_per_thread(tmp_path, monkeypatch):
    """``AdapterFactory.create`` re-tunes and re-checks the schema on every build;
    one bundle per worker thread, rebuilt only when the thread's handle changes."""
    owner = _owner_connection_on_this_thread(tmp_path, monkeypatch)
    monkeypatch.setattr(query_pipeline, "_thread_bundles", threading.local(), raising=False)
    first = query_pipeline._bundle_for_thread_conn(owner)
    assert query_pipeline._bundle_for_thread_conn(owner) is first
    other = sqlite3.connect(str(tmp_path / "engine.db"), check_same_thread=False)
    try:
        assert query_pipeline._bundle_for_thread_conn(other) is not first
    finally:
        other.close()


def test_injected_adapters_keep_their_own_store_off_the_loop():
    """A harness that injected its adapters never hops, so the worker marker is
    never set and ``_thread_session_store`` must hand back the injected store —
    never a handle to whatever database ``state`` happens to point at."""
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.query.runtime import adapter_bundle_for_conn

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        orchestrator = QueryPipelineOrchestrator(adapters=adapter_bundle_for_conn(conn))
        assert orchestrator._worker_db_connection() is None
        assert orchestrator._thread_session_store() is orchestrator._session_store()
    finally:
        conn.close()
