"""Engine tests for Usage Inbox write_id ack and dedupe."""

from __future__ import annotations

import sqlite3
import threading
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from topos.ingestion.usage_inbox_dedupe import get_prior_delivery, record_delivery
from topos.core import handlers


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sqlite_conn(tmp_path, monkeypatch):
    """This thread's connection. Every other thread gets one of its own, as in production.

    `core.state.get_db_connection` hands each thread its own connection to the same
    WAL file. The dedupe lookups run on `asyncio.to_thread` workers (their write gate
    must not be taken on the event loop), and so do the sweeps and claims of the
    pipeline worker a dedupe hit starts. One `check_same_thread=False` handle shared
    between them is two threads on one sqlite3 connection: on Python 3.12 the thread
    that loses raises "bad parameter or other API misuse" (SQLITE_MISUSE), and a
    lookup that swallows it reads as "never delivered".
    """
    db_path = tmp_path / "test.db"
    local = threading.local()

    def _conn():
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            local.conn = conn
        return conn

    monkeypatch.setattr(
        "topos.ingestion.usage_inbox_dedupe.get_db_connection",
        _conn,
    )
    monkeypatch.setattr(
        "topos.core.handlers.get_db_connection",
        _conn,
    )
    conn = _conn()
    yield conn
    # A dedupe hit whose derivation never finished starts the real pipeline worker
    # (TOPOS_PIPELINE_WORKER defaults on), and its loops go on sweeping and claiming
    # after the handler returns. Stop them here, on the test's loop: left to the
    # loop's own teardown, they outlive this fixture whenever the loop does. Then
    # close only this thread's handle. A stopped loop's last hop can still be
    # running on its thread, and closing a handle under a live thread segfaults
    # CPython's sqlite3; the workers' handles go when their threads do.
    from topos.pipeline.job_runner import stop_pipeline_worker

    await stop_pipeline_worker()
    conn.close()


async def test_write_id_in_success_response(sqlite_conn, monkeypatch) -> None:
    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.ingest_ui_payload",
        AsyncMock(return_value={"status": "ok"}),
    )
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    message = {
        "id": "req-1",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-abc",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "hello"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }
    result = await handlers.handle_control_plane_request(message)
    assert result["status"] == "ok"
    assert result["payload"]["write_id"] == "write-abc"
    assert result["payload"]["records_processed"] == 1


async def test_write_id_dedupe_skips_reingest(sqlite_conn, monkeypatch) -> None:
    record_delivery("write-dup", records_processed=2, records_total=2)
    assert get_prior_delivery("write-dup") == {"records_processed": 2, "records_total": 2}

    ingest_mock = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_ui_payload", ingest_mock)

    message = {
        "id": "req-2",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-dup",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "hello"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }
    result = await handlers.handle_control_plane_request(message)
    assert result["status"] == "ok"
    assert result["payload"]["deduplicated"] is True
    ingest_mock.assert_not_awaited()
