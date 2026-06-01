from __future__ import annotations

import asyncio
import json

import pytest

import topos.sync.client as sync_client_module
from topos.sync.client import SyncClient


class FakeWebSocket:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws
        self.last_headers = None

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __call__(self, url, additional_headers=None, ssl=None):
        _ = (url, ssl)
        self.last_headers = additional_headers
        return self


@pytest.mark.asyncio
async def test_sync_client_connects_and_reports_status(monkeypatch):
    ws = FakeWebSocket([json.dumps({"type": "sync_connected"})])
    connect = FakeConnect(ws)
    monkeypatch.setattr(sync_client_module, "connect", connect)

    client = SyncClient(
        sync_url="ws://example/ws/sync",
        api_key="test-key",
        user_id="user-1",
        dataset_id="dataset-1",
        on_op_received=lambda _op: None,
        verify_ssl=False,
    )
    task = asyncio.create_task(client._run())
    ready = await client.wait_until_connected(timeout_s=0.2)
    assert ready is True
    client._stop.set()
    await asyncio.wait_for(task, timeout=1)
    status = client.get_connection_status()
    assert status["attempt"] >= 1
    assert connect.last_headers == {"Authorization": "Bearer test-key"}


@pytest.mark.asyncio
async def test_sync_client_retries_cursor_send(monkeypatch):
    ws = FakeWebSocket(
        [
            json.dumps({"type": "sync_connected"}),
            json.dumps({"type": "sync_op", "op": {"hlc_ts": "123", "op_id": "op-1"}}),
        ]
    )
    send_calls = {"count": 0}

    async def flaky_send(message):
        send_calls["count"] += 1
        if send_calls["count"] == 2:
            raise RuntimeError("transient send failure")
        ws.sent.append(message)

    ws.send = flaky_send  # type: ignore[assignment]
    connect = FakeConnect(ws)
    monkeypatch.setattr(sync_client_module, "connect", connect)

    client = SyncClient(
        sync_url="ws://example/ws/sync",
        api_key="test-key",
        user_id="user-1",
        dataset_id="dataset-1",
        on_op_received=lambda _op: None,
        verify_ssl=False,
    )
    task = asyncio.create_task(client._run())
    await asyncio.sleep(0.1)
    client._stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert any("sync_cursor" in payload for payload in ws.sent)
