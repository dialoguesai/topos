"""``run_db_read``/``run_db_write`` must use the WORKER's own connection.

The root cause of the 2026-08-17 outage: a handler resolved the event loop's
``sqlite3.Connection`` and handed it to ``run_db_read``, which ran the work on an
``asyncio.to_thread`` worker. Loop thread and worker then executed on one
Connection at once, and CPython's per-connection statement cache — an
unsynchronized C LRU — went inconsistent. Its eviction path deleted a key twice
and every later ``execute`` on that handle raised ``KeyError(('<sql>',))``,
naming a statement the caller had never issued.

A caller-passed connection is what defeats the thread-local ``get_db_connection``,
so these tests assert the negative: whatever the caller holds, the worker resolves
its own.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import topos.core.handlers as hub
from topos.core.handlers.common import run_db_read, run_db_write


@pytest.mark.asyncio
async def test_run_db_read_resolves_its_own_connection(monkeypatch):
    resolved_on: list[int] = []
    sentinel = object()

    def _fake_get_conn():
        resolved_on.append(threading.get_ident())
        return sentinel

    monkeypatch.setattr(hub, "get_db_connection", _fake_get_conn)

    loop_thread = threading.get_ident()
    seen = {}

    def _work(conn, marker):
        seen["conn"] = conn
        seen["thread"] = threading.get_ident()
        seen["marker"] = marker
        return "done"

    assert await run_db_read(_work, "m") == "done"

    # The connection came from the hub, not from the caller...
    assert seen["conn"] is sentinel
    assert seen["marker"] == "m"
    # ...it ran off the event loop...
    assert seen["thread"] != loop_thread
    # ...and it was resolved ON the worker, not handed across the boundary.
    assert resolved_on == [seen["thread"]]


@pytest.mark.asyncio
async def test_run_db_write_resolves_its_own_connection(monkeypatch):
    resolved_on: list[int] = []
    sentinel = object()

    def _fake_get_conn():
        resolved_on.append(threading.get_ident())
        return sentinel

    monkeypatch.setattr(hub, "get_db_connection", _fake_get_conn)

    seen = {}

    def _work(conn, *, kw):
        seen["conn"] = conn
        seen["thread"] = threading.get_ident()
        seen["kw"] = kw
        return 7

    assert await run_db_write(_work, kw="v") == 7
    assert seen["conn"] is sentinel
    assert seen["kw"] == "v"
    assert seen["thread"] != threading.get_ident()
    assert resolved_on == [seen["thread"]]


@pytest.mark.asyncio
async def test_concurrent_reads_each_get_their_own_resolution(monkeypatch):
    """Two overlapping reads must not end up sharing one handle."""
    per_thread: dict[int, object] = {}
    lock = threading.Lock()

    def _fake_get_conn():
        ident = threading.get_ident()
        with lock:
            # Mirrors the real thread-local: one connection per thread.
            return per_thread.setdefault(ident, object())

    monkeypatch.setattr(hub, "get_db_connection", _fake_get_conn)

    started = threading.Barrier(2, timeout=10)

    def _work(conn):
        # Force genuine overlap; without the barrier the pool can serialize.
        started.wait()
        return (threading.get_ident(), id(conn))

    results = await asyncio.gather(run_db_read(_work), run_db_read(_work))

    threads = {t for t, _ in results}
    conns = {c for _, c in results}
    assert len(threads) == 2, "expected the two reads to overlap on distinct threads"
    assert len(conns) == 2, "overlapping reads shared one connection"


@pytest.mark.asyncio
async def test_routine_handlers_never_pass_a_connection_into_a_worker():
    """Guard the specific shape that caused the outage, at its call sites.

    ``routines.py`` still resolves a connection to answer "is a database
    configured", so the presence of ``get_db_connection`` is fine; passing that
    handle on to a worker is not.
    """
    import inspect

    from topos.core.handlers import routines

    source = inspect.getsource(routines)
    for call in ("run_db_read(", "run_db_write("):
        idx = 0
        while True:
            idx = source.find(call, idx)
            if idx == -1:
                break
            # Look at the argument list up to the matching close paren.
            window = source[idx : source.find(")", idx)]
            assert ", conn" not in window and "(conn" not in window, (
                f"{call} in routines.py is being handed a caller-resolved "
                f"connection: {window!r}"
            )
            idx += len(call)
