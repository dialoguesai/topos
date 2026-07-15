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
async def test_wait_for_stop_or_timeout_does_not_propagate_timeout():
    """Backoff wait must swallow wait_for timeouts so the reconnect loop stays alive.

    On Python 3.10, asyncio.TimeoutError is not builtins.TimeoutError; catching only
    the builtin lets the exception kill ControlPlaneClient._run.
    """

    async def handler(_message):
        return None

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=False,
    )
    await client._wait_for_stop_or_timeout(0.01)
    assert not client._stop.is_set()


@pytest.mark.asyncio
async def test_reconnect_loop_survives_backoff_timeout(monkeypatch):
    """After a clean disconnect, _run must keep looping instead of dying on backoff."""
    connect_calls = {"n": 0}

    class HangForeverWebSocket(FakeWebSocket):
        def __init__(self):
            super().__init__([])
            self._gate = asyncio.Event()

        async def __anext__(self):
            await self._gate.wait()
            raise StopAsyncIteration

    class FlappingConnect:
        def __init__(self):
            self.last_headers = None
            self.last_ssl = None

        def __call__(self, url, additional_headers=None, ssl=None, **kwargs):
            _ = (url, kwargs)
            self.last_headers = additional_headers
            self.last_ssl = ssl
            return self

        async def __aenter__(self):
            connect_calls["n"] += 1
            if connect_calls["n"] == 1:
                return FakeWebSocket([])
            return HangForeverWebSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(control_plane_client, "connect", FlappingConnect())

    async def handler(_message):
        return None

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=False,
    )
    client._backoff = control_plane_client.ExponentialBackoff(
        control_plane_client.ResilienceConfig(initial_backoff_s=0.01, max_backoff_s=0.01, jitter_ratio=0.0)
    )

    client.start()
    for _ in range(50):
        if connect_calls["n"] >= 2 and client._task and not client._task.done():
            break
        await asyncio.sleep(0.02)
    else:
        status = {
            "connect_calls": connect_calls["n"],
            "task_done": client._task.done() if client._task else None,
            "task_exc": repr(client._task.exception()) if client._task and client._task.done() else None,
        }
        await client.stop()
        raise AssertionError(f"reconnect loop did not survive backoff: {status}")

    assert not client._task.done()
    await client.stop()


@pytest.mark.asyncio
async def test_send_message_restarts_background_task_if_stopped(monkeypatch):
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

    # Simulate an unexpected task exit while the app is still running.
    client._task = asyncio.create_task(asyncio.sleep(0))
    await client._task
    assert client._task.done()

    await client.send_message({"type": "engine_register", "id": "queued"})

    assert client._task is not None
    assert not client._task.done()
    client._stop.set()
    await asyncio.wait_for(client._task, timeout=1)


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


@pytest.mark.asyncio
async def test_control_plane_client_fast_lane_bypasses_saturation_gate(monkeypatch):
    """UI-critical reads must not be dropped when the inbound queue is saturated."""
    request_slow = {"id": "req-slow", "type": "alpha", "payload": {}}
    request_fast = {"id": "req-fast", "type": "list_routine_runs", "payload": {}}
    ws = FakeWebSocket([json.dumps(request_slow), json.dumps(request_fast)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    gate = asyncio.Event()

    async def handler(message):
        if message["id"] == "req-slow":
            await gate.wait()
        return {"id": message["id"], "status": "ok", "payload": {"runs": []}}

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
    assert any(msg.get("id") == "req-fast" and msg.get("status") == "ok" for msg in sent)
    assert not any(
        msg.get("id") == "req-fast" and msg.get("status") == "error" for msg in sent
    )


@pytest.mark.asyncio
async def test_control_plane_client_replies_pong_to_ping_without_handler():
    ws = FakeWebSocket([])
    handler_called = False

    async def handler(_message):
        nonlocal handler_called
        handler_called = True
        return None

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=False,
    )

    await client._handle_message(ws, {"type": "ping"})
    assert not handler_called
    assert json.loads(ws.sent[0]) == {"type": "pong"}

    ws.sent.clear()
    await client._handle_message(ws, {"type": "ping", "id": "ping-1"})
    assert not handler_called
    assert json.loads(ws.sent[0]) == {"type": "pong", "id": "ping-1"}


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
