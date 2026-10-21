"""Content-free query serve spans: closed labels, and a duration that is never a first token."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from topos.serve.spans import (
    SpanRefused,
    finish_request,
    flush_stages,
    publish_stages,
    start_request,
    validate_span,
    wait_for_writes,
)
from topos.storage.db.migrations.registry import MIGRATIONS, max_migration_order
from topos.storage.db.migrations.serve_spans_v1 import create_serve_spans

PROBE = "the owner asked about a clinic visit next week"


def test_migration_89_creates_the_table():
    spec = next(item for item in MIGRATIONS if item.id == "serve_spans_v1")
    assert spec.order == 89 and not spec.always_run
    assert max_migration_order() >= spec.order
    conn = sqlite3.connect(":memory:")
    create_serve_spans(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(serve_spans)")}
    assert {"span_id", "parent_span_id", "recorded_at", "kind", "name", "duration_ms", "ttfb_ms", "outcome"} <= columns


def test_a_sentence_and_a_database_first_token_are_refused():
    parent = str(uuid4())
    with pytest.raises(SpanRefused) as refused:
        validate_span(kind="db", name=PROBE, duration_ms=12, outcome="ok", parent_span_id=parent)
    assert refused.value.field == "name"
    assert PROBE not in str(refused.value)
    with pytest.raises(SpanRefused) as timed:
        validate_span(kind="db", name="retrieval", duration_ms=12, outcome="ok", ttfb_ms=4, parent_span_id=parent)
    assert timed.value.field == "ttfb_ms"


def _rows(conn):
    return conn.execute(
        "SELECT span_id, parent_span_id, kind, name, duration_ms, ttfb_ms, outcome FROM serve_spans ORDER BY kind"
    ).fetchall()


def test_an_inference_turn_records_duration_and_leaves_ttfb_empty():
    conn = sqlite3.connect(":memory:")
    started, token = start_request()
    publish_stages(12.4, 80.6, "ok")
    flush_stages(conn)
    finish_request(started, "ok", token, conn)
    rows = _rows(conn)
    assert [row[2] for row in rows] == ["db", "llm", "request"]
    request = next(row for row in rows if row[2] == "request")
    assert request[1] is None and request[4] >= 0 and request[5] is None and request[6] == "ok"
    db, llm = rows[0], rows[1]
    assert db[1] == request[0] and db[3] == "retrieval" and db[4] == 12 and db[5] is None
    assert llm[1] == request[0] and llm[3] == "query_inference" and llm[4] == 81 and llm[5] is None


def test_a_facts_turn_omits_the_llm_span():
    conn = sqlite3.connect(":memory:")
    started, token = start_request()
    publish_stages(9.2, None, "ok")
    flush_stages(conn)
    finish_request(started, "ok", token, conn)
    kinds = [row[2] for row in _rows(conn)]
    assert kinds == ["db", "request"]


def test_a_failed_query_stores_the_error():
    conn = sqlite3.connect(":memory:")
    started, token = start_request()
    finish_request(started, "error", token, conn)
    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0][2] == "request" and rows[0][6] == "error"


@pytest.mark.asyncio
async def test_handle_query_keeps_an_error_row(monkeypatch):
    # Written by the span worker thread, not the loop: the handle must admit that thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    def explode(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr("topos.query.manifest_validation.resolve_scope_manifest", explode)
    from topos.core.handlers.query import handle_query

    response = await handle_query({"id": "q-1", "type": "query", "payload": {"query": PROBE, "scope_id": "notes"}})
    assert response["status"] == "error"
    assert wait_for_writes(5.0)
    rows = _rows(conn)
    assert len(rows) == 1 and rows[0][6] == "error"
    assert PROBE not in str(rows)


def _hold_gate():
    """Take the write gate on another thread until ``release`` is set. Returns (release, thread)."""
    from topos.storage.db.write_gate import db_write_lock

    held, release = threading.Event(), threading.Event()

    def hold():
        with db_write_lock():
            held.set()
            release.wait(10.0)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(5.0)
    return release, thread


@pytest.mark.asyncio
async def test_a_held_write_gate_does_not_delay_finish_request_on_the_loop(caplog):
    """21 Oct 2026: the review pass held the gate 60 to 109 s and every query waited here, on the loop.
    Now the loop hands the rows over and returns; the worker waits, the loop does not."""
    from topos.storage.db.write_gate import reset_loop_warning_state

    reset_loop_warning_state()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    release, thread = _hold_gate()
    try:
        ticks = 0

        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.ensure_future(tick())
        started, token = start_request()
        publish_stages(3.0, 4.0, "ok")
        with caplog.at_level(logging.WARNING, logger="topos.storage.db.write_gate"):
            before = time.perf_counter()
            flush_stages(conn)
            finish_request(started, "ok", token, conn)
            elapsed = time.perf_counter() - before
            await asyncio.sleep(0.25)
        ticker.cancel()
        assert elapsed < 0.2, f"finish_request waited {elapsed:.2f}s on the loop with the gate held"
        assert ticks >= 10, "the loop stalled while the gate was held"
        assert not wait_for_writes(0.05), "rows were written while the gate was still held"
        assert not [r for r in caplog.records if "event-loop thread" in r.getMessage()]
    finally:
        release.set()
        thread.join(5.0)
    assert wait_for_writes(5.0)
    rows = _rows(conn)
    assert [row[2] for row in rows] == ["db", "llm", "request"]
    request = next(row for row in rows if row[2] == "request")
    assert all(row[1] == request[0] for row in rows if row[2] != "request")
