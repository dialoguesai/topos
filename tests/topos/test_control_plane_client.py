import asyncio
import json
import logging

import pytest

import topos.control_plane_client as control_plane_client
from topos.control_plane_client import ControlPlaneClient


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

    async def close(self, code=1000):
        _ = code
        self.closed = True


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws
        self.last_headers = None
        self.last_ssl = None

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __call__(self, url, additional_headers=None, ssl=None, **kwargs):
        _ = (url, kwargs)
        self.last_headers = additional_headers
        self.last_ssl = ssl
        return self


@pytest.mark.asyncio
async def test_control_plane_client_sends_response(monkeypatch):
    request = {"id": "req-1", "type": "healthcheck", "payload": {}}
    ws = FakeWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    handled = asyncio.Event()

    client = None

    async def handler(message):
        nonlocal client
        assert message["type"] == "healthcheck"
        handled.set()
        if client:
            client._stop.set()
        return {"id": message["id"], "status": "ok", "payload": {"status": "ok"}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    task = asyncio.create_task(client._run())
    await asyncio.wait_for(handled.wait(), timeout=1)
    client._stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert connect.last_headers == {"Authorization": "Bearer test-key"}
    assert ws.sent
    response = json.loads(ws.sent[0])
    assert response["status"] == "ok"
    status = client.get_connection_status()
    assert status["attempt"] >= 1
    assert status["state"] in {"idle", "connected"}


@pytest.mark.asyncio
async def test_control_plane_client_queues_presence_until_connected(monkeypatch):
    ws = FakeWebSocket([])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    async def handler(_message):
        return None

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=False,
    )
    await client.send_message({"type": "engine_register", "id": "queued"})
    assert client.get_connection_status()["outbox_depth"] == 1

    task = asyncio.create_task(client._run())
    await asyncio.sleep(0.05)
    client._stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert any("engine_register" in payload for payload in ws.sent)


@pytest.mark.asyncio
async def test_control_plane_client_sends_busy_error_when_saturated(monkeypatch):
    request_a = {"id": "req-a", "type": "alpha", "payload": {}}
    request_b = {"id": "req-b", "type": "beta", "payload": {}}
    ws = FakeWebSocket([json.dumps(request_a), json.dumps(request_b)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    gate = asyncio.Event()

    async def handler(message):
        if message["id"] == "req-a":
            await gate.wait()
        return {"id": message["id"], "status": "ok"}

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=False,
    )
    client._inbound_max_pending = 1
    client._inbound_semaphore = asyncio.Semaphore(1)
    task = asyncio.create_task(client._run())
    await asyncio.sleep(0.05)
    gate.set()
    client._stop.set()
    await asyncio.wait_for(task, timeout=1)
    sent = [json.loads(msg) for msg in ws.sent]
    assert any(msg.get("id") == "req-b" and msg.get("status") == "error" for msg in sent)


def test_record_failure_logs_endpoint_context(monkeypatch, caplog):
    async def handler(_message):
        return None

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )

    monkeypatch.setattr(
        control_plane_client,
        "classify_connection_error",
        lambda _exc: ("upgrade_5xx", "server rejected WebSocket connection: HTTP 502"),
    )

    with caplog.at_level(logging.WARNING, logger="topos.control_plane_client"):
        client._record_failure(RuntimeError("boom"))

    assert "endpoint=wss://cp.example/ws/engine" in caplog.text
    assert "event=connection_failed" in caplog.text
