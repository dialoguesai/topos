"""Polling must never park the app loop behind the database write gate."""
import asyncio
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from topos.core import state
from topos.core.handlers import config


@pytest.mark.asyncio
async def test_counts_use_a_worker_owned_connection_and_keep_loop_responsive(monkeypatch):
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    connection = object()
    worker_threads = []

    def get_connection():
        worker_threads.append(threading.get_ident())
        return connection

    def uma_counts(conn, owner, days):
        assert conn is connection
        assert owner == "owner"
        assert days == 90
        entered.set()
        assert release.wait(3)
        return {"by_app": []}

    monkeypatch.setattr(config.hub, "get_db_connection", get_connection)
    monkeypatch.setattr(config, "get_uma_request_counts", uma_counts)
    monkeypatch.setattr(config, "get_mcp_request_counts", lambda conn, days: {"total": 1})
    task = asyncio.create_task(config.handle_get_request_counts({"id": "counts", "payload": {"owner_user_id": "owner"}}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done()
        assert worker_threads[0] != loop_thread
    finally:
        release.set()
    result = await task
    assert result["status"] == "ok"
    assert result["payload"]["mcp"]["total"] == 1


def test_warm_request_counts_ensure_paths_do_not_acquire_write_gate(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {state.UMA_ACCESS_REQUESTS_TABLE} (owner_user_id TEXT, created_at TEXT, app_id TEXT)")
    conn.execute(f"CREATE TABLE {state.MCP_REQUEST_LOG_TABLE} (requested_at TEXT)")
    conn.execute(f"CREATE INDEX idx_uma_access_requests_owner_created ON {state.UMA_ACCESS_REQUESTS_TABLE}(owner_user_id, created_at)")
    conn.execute(f"CREATE INDEX idx_mcp_request_log_requested_at ON {state.MCP_REQUEST_LOG_TABLE}(requested_at)")

    @contextmanager
    def forbidden_write():
        raise AssertionError("steady-state polling acquired the write gate")
        yield

    monkeypatch.setattr(state, "with_db_write", forbidden_write)
    try:
        state._ensure_uma_access_requests_indexes(conn)
        state._ensure_mcp_request_log_indexes(conn)
        assert state._migrate_legacy_browser_plugin_app_ids(conn) == 0
    finally:
        conn.close()
