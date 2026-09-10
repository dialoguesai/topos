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

        async def close(self, code=1000):
            # A real websocket's close() ends the iteration blocked in recv.
            # Without this the fake parked `_run` on `_gate` forever: stop()
            # set `_stop` (which `_run` only reads BETWEEN messages), waited out
            # its full 10s join, and returned with the thread still alive.
            await super().close(code)
            self._gate.set()

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
    # start() runs the client on its own thread now; the guarded behavior is
    # unchanged — after a clean disconnect the loop reconnects instead of dying.
    for _ in range(100):
        if connect_calls["n"] >= 2 and client._thread and client._thread.is_alive():
            break
        await asyncio.sleep(0.02)
    else:
        status = {
            "connect_calls": connect_calls["n"],
            "thread_alive": client._thread.is_alive() if client._thread else None,
        }
        await client.stop()
        raise AssertionError(f"reconnect loop did not survive backoff: {status}")

    assert client._thread.is_alive()
    thread = client._thread
    await client.stop()
    # The property this test exists for includes getting OUT: a stop() that
    # returns with the thread still parked is the leak the suite could not see.
    assert not thread.is_alive(), "stop() returned with the client thread still running"


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

    # Simulate an unexpected exit while the app is still running.
    client._task = asyncio.create_task(asyncio.sleep(0))
    await client._task
    assert client._task.done()

    await client.send_message({"type": "engine_register", "id": "queued"})

    # The self-heal restart now brings the client up on its own thread.
    assert client._thread is not None
    for _ in range(100):
        if client._thread.is_alive():
            break
        await asyncio.sleep(0.02)
    assert client._thread.is_alive()
    await client.stop()


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


def test_ui_bootstrap_types_bypass_inbound_saturation():
    fast = control_plane_client._FAST_INBOUND_MESSAGE_TYPES
    assert "get_runtime_bootstrap" in fast
    assert "get_upgrade_status" in fast
    assert "healthcheck" in fast


class StayOpenWebSocket(FakeWebSocket):
    """Delivers its scripted messages, then holds the connection open."""

    def __init__(self, messages):
        super().__init__(messages)
        self.sent_at = []
        self._hold = None

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._hold is None:
            self._hold = asyncio.Event()
        await self._hold.wait()
        raise StopAsyncIteration

    async def send(self, message):
        import time as _time

        self.sent_at.append(_time.monotonic())
        self.sent.append(message)

    async def close(self, code=1000):
        await super().close(code)
        if self._hold is not None:
            self._hold.set()


@pytest.mark.asyncio
async def test_threaded_client_answers_ping_while_app_loop_is_stalled(monkeypatch):
    """The regression that killed every first-run install: model prewarm
    starved the app loop for minutes, the node missed the control plane's
    pong deadline, and the CP executed the connection ~150s after register.
    With the client on its own thread, a protocol ping must be answered even
    while the app loop is blocked solid.
    """
    import time

    ping = {"id": "ping-1", "type": "ping"}
    ws = StayOpenWebSocket([json.dumps(ping)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    async def handler(message):  # pragma: no cover - ping path never reaches it
        return {"id": message.get("id"), "status": "ok"}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        stall_started = time.monotonic()
        # Stall the app loop the way prewarm does: synchronously.
        time.sleep(1.5)
        # The pong must have been sent DURING the stall, not after it.
        assert ws.sent, "no pong was sent at all"
        pong = json.loads(ws.sent[0])
        assert pong["type"] == "pong" and pong["id"] == "ping-1"
        assert ws.sent_at[0] < stall_started + 1.0, (
            "pong waited for the app loop — the client is not isolated from stalls"
        )
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_threaded_client_runs_handler_on_the_app_loop(monkeypatch):
    """Engine handlers touch app-loop state; the thread must marshal them back."""
    request = {"id": "req-9", "type": "healthcheck", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    app_loop = asyncio.get_running_loop()
    seen = {}
    handled = asyncio.Event()

    async def handler(message):
        seen["loop"] = asyncio.get_running_loop()
        app_loop.call_soon_threadsafe(handled.set) if asyncio.get_running_loop() is not app_loop else handled.set()
        return {"id": message["id"], "status": "ok", "payload": {}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        await asyncio.wait_for(handled.wait(), timeout=5.0)
        assert seen["loop"] is app_loop, "handler ran off the app loop"
        for _ in range(50):
            if any('"status": "ok"' in m or '"status":"ok"' in m for m in ws.sent):
                break
            await asyncio.sleep(0.1)
        assert any("req-9" in m for m in ws.sent), "response never sent back"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_threaded_client_stop_joins_the_thread(monkeypatch):
    ws = StayOpenWebSocket([])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    async def handler(message):  # pragma: no cover
        return None

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    client.start()
    assert await client.wait_until_connected(timeout_s=5.0)
    thread = client._thread
    assert thread is not None and thread.is_alive()
    await client.stop()
    assert not thread.is_alive(), "client thread failed to exit on stop()"
    assert client.get_connection_status()["state"] == "idle"


@pytest.mark.asyncio
async def test_get_device_info_answered_from_snapshot_when_app_loop_stalls(monkeypatch):
    """First-run model prewarm freezes the app loop for minutes; the relayed
    get_device_info must still answer — from the seeded snapshot — or setup
    cannot learn the machine name."""
    request = {"id": "di-1", "type": "get_device_info", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)
    monkeypatch.setattr(ControlPlaneClient, "_SNAPSHOT_ANSWER_DEADLINE_S", 0.5)

    stall = asyncio.Event()

    async def stalled_handler(message):
        await stall.wait()  # the app loop "never" answers
        return {"id": message["id"], "status": "ok", "payload": {"system": {"computer_name": "fresh"}}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=stalled_handler,
        verify_ssl=True,
    )
    client.set_device_info_snapshot({"system": {"computer_name": "q4"}, "dataset_id": "d1"})
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        for _ in range(80):
            if ws.sent:
                break
            await asyncio.sleep(0.1)
        assert ws.sent, "no snapshot answer while the handler was stalled"
        resp = json.loads(ws.sent[0])
        assert resp["id"] == "di-1"
        assert resp["status"] == "ok"
        assert resp["payload"]["system"]["computer_name"] == "q4"
        assert resp["payload"]["snapshot_stale"] is True
        assert resp["type"] == "get_device_info"
    finally:
        stall.set()
        await client.stop()


@pytest.mark.asyncio
async def test_get_device_info_empty_snapshot_answers_stub_instead_of_waiting(monkeypatch):
    request = {"id": "di-empty", "type": "get_device_info", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)
    monkeypatch.setattr(ControlPlaneClient, "_SNAPSHOT_ANSWER_DEADLINE_S", 0.3)

    stall = asyncio.Event()

    async def stalled_handler(message):
        await stall.wait()
        return {"id": message["id"], "status": "ok", "payload": {"system": {"computer_name": "late"}}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=stalled_handler,
        verify_ssl=True,
    )
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        for _ in range(40):
            if ws.sent:
                break
            await asyncio.sleep(0.1)
        assert ws.sent, "empty snapshot must not wait for the stalled handler"
        resp = json.loads(ws.sent[0])
        assert resp["id"] == "di-empty"
        assert resp["status"] == "ok"
        assert resp["payload"]["snapshot_empty"] is True
        assert resp["payload"]["snapshot_stale"] is True
    finally:
        stall.set()
        await client.stop()


@pytest.mark.asyncio
async def test_healthcheck_answered_from_client_thread_when_app_loop_stalls(monkeypatch):
    request = {"id": "hc-1", "type": "healthcheck", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)
    monkeypatch.setattr(ControlPlaneClient, "_HEALTHCHECK_ANSWER_DEADLINE_S", 0.3)

    stall = asyncio.Event()

    async def stalled_handler(message):
        await stall.wait()
        return {"id": message["id"], "status": "ok", "payload": {"ok": True, "db_ok": True}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=stalled_handler,
        verify_ssl=True,
    )
    client.set_healthcheck_snapshot({"ok": True, "db_ok": False})
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        for _ in range(40):
            if ws.sent:
                break
            await asyncio.sleep(0.1)
        assert ws.sent, "healthcheck must answer from the client thread within the CP timeout"
        resp = json.loads(ws.sent[0])
        assert resp["id"] == "hc-1"
        assert resp["status"] == "ok"
        assert resp["type"] == "healthcheck"
        assert resp["payload"]["ok"] is True
        assert resp["payload"]["snapshot_stale"] is True
        assert resp["payload"]["db_ok"] is False
    finally:
        stall.set()
        await client.stop()


@pytest.mark.asyncio
async def test_terminal_frame_echoes_inbound_type(monkeypatch):
    request = {"id": "echo-1", "type": "get_device_info", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    async def healthy_handler(message):
        return {"id": message["id"], "status": "ok", "payload": {"system": {"computer_name": "q4"}}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=healthy_handler,
        verify_ssl=True,
    )
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        for _ in range(80):
            if ws.sent:
                break
            await asyncio.sleep(0.1)
        resp = json.loads(ws.sent[0])
        assert resp["type"] == "get_device_info"
        assert resp["status"] == "ok"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_get_device_info_prefers_the_real_answer_when_the_loop_is_healthy(monkeypatch):
    request = {"id": "di-2", "type": "get_device_info", "payload": {}}
    ws = StayOpenWebSocket([json.dumps(request)])
    connect = FakeConnect(ws)
    monkeypatch.setattr(control_plane_client, "connect", connect)

    async def healthy_handler(message):
        return {"id": message["id"], "status": "ok", "payload": {"system": {"computer_name": "fresh"}}}

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=healthy_handler,
        verify_ssl=True,
    )
    client.set_device_info_snapshot({"system": {"computer_name": "stale"}})
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        for _ in range(80):
            if ws.sent:
                break
            await asyncio.sleep(0.1)
        resp = json.loads(ws.sent[0])
        assert resp["payload"]["system"]["computer_name"] == "fresh"
        assert "snapshot_stale" not in resp["payload"]
        # And the fresh answer refreshed the snapshot.
        assert client._device_info_snapshot["system"]["computer_name"] == "fresh"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_connect_sets_a_real_pong_deadline(monkeypatch):
    """A silently dead socket must become an exception, not a permanent wedge.

    ping_timeout=None was a workaround for handlers blocking this loop and
    missing pong deadlines — a reason removed when the client got its own
    thread. Left in place it turned every silent connection death into a node
    that reported "connected" forever while the control plane had no socket:
    `async for raw in ws` blocks, nothing raises, no reconnect is scheduled.
    Observed live 2026-08-08, wedged 25 minutes.
    """
    ws = StayOpenWebSocket([])
    connect = FakeConnect(ws)
    captured = {}

    def capturing_connect(url, additional_headers=None, ssl=None, **kwargs):
        captured.update(kwargs)
        return connect(url, additional_headers=additional_headers, ssl=ssl, **kwargs)

    monkeypatch.setattr(control_plane_client, "connect", capturing_connect)

    async def handler(_message):
        return None

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    client.start()
    try:
        assert await client.wait_until_connected(timeout_s=5.0)
        assert captured.get("ping_timeout") is not None, "a None pong deadline wedges forever"
        assert captured["ping_timeout"] == control_plane_client.PING_TIMEOUT_SECONDS
        assert captured["ping_interval"] == control_plane_client.PING_INTERVAL_SECONDS
        # Between the two failure modes: long enough that a slow-but-healthy
        # node is never killed (20s killed one every 50s in production), short
        # enough to beat the control plane's own eviction (30s + 120s), so the
        # node detects a real death itself instead of being evicted.
        detection = (
            control_plane_client.PING_INTERVAL_SECONDS + control_plane_client.PING_TIMEOUT_SECONDS
        )
        assert detection >= 60.0, "a tight deadline kills healthy connections mid-request"
        assert detection < 150.0, "must notice before the control plane evicts us"
    finally:
        await client.stop()


def _reap_client_thread(client, thread) -> None:
    """Stop a client thread a failing assertion left behind, so it cannot
    reconnect through a later test's patched `connect` and poison that test."""
    if thread is None or not thread.is_alive():
        return
    client._stop_requested = True
    loop = client._loop
    if loop is not None:
        asyncio.run_coroutine_threadsafe(client._shutdown_on_client_loop(), loop)
    thread.join(5.0)


@pytest.mark.asyncio
async def test_stop_before_the_thread_has_a_loop_still_stops_the_thread(monkeypatch):
    """stop() must not return while the client thread is still running.

    If stop() runs before `_thread_main` has published `_loop`, it cannot reach
    the client loop, so it sets the `_stop` Event from `__init__` -- which the
    thread then REPLACES with a fresh, unset one and reconnects forever. Measured
    on the unfixed client: 25 of 40 back-to-back start()/stop() calls left the
    thread alive. Each zombie reconnects through the module-level `connect`, so
    when its backoff fires during a later test that has patched `connect`, it
    lands on that test's fake socket, eats its scripted message and pins the
    socket's Event to its own loop. That is the CI failure of
    `test_get_device_info_prefers_the_real_answer_when_the_loop_is_healthy`
    ("Event ... is bound to a different event loop", 2026-09-09 and 09-10).

    Deterministic, not lucky: the thread is held back so stop() is guaranteed to
    run before the loop exists.
    """
    import time as _time

    ws = StayOpenWebSocket([])
    monkeypatch.setattr(control_plane_client, "connect", FakeConnect(ws))

    async def handler(message):
        return None

    client = ControlPlaneClient(
        control_plane_url="wss://cp.example/ws/engine",
        api_key="test-key",
        handler=handler,
        verify_ssl=True,
    )
    real_thread_main = client._thread_main

    def late_thread_main():
        _time.sleep(0.2)  # stop() runs before this thread publishes its loop
        real_thread_main()

    monkeypatch.setattr(client, "_thread_main", late_thread_main)
    client.start()
    thread = client._thread
    try:
        await client.stop()
        assert thread is not None
        assert not thread.is_alive(), "stop() returned with the client thread still running"
    finally:
        _reap_client_thread(client, thread)


@pytest.mark.asyncio
async def test_back_to_back_start_stop_leaves_no_client_thread(monkeypatch):
    """The same property without the forced delay: the natural race, repeated."""
    leftovers = []
    for _ in range(10):
        ws = StayOpenWebSocket([])
        monkeypatch.setattr(control_plane_client, "connect", FakeConnect(ws))

        async def handler(message):
            return None

        client = ControlPlaneClient(
            control_plane_url="wss://cp.example/ws/engine",
            api_key="test-key",
            handler=handler,
            verify_ssl=True,
        )
        client.start()
        thread = client._thread
        await client.stop()
        if thread is not None and thread.is_alive():
            leftovers.append((client, thread))
    for client, thread in leftovers:
        _reap_client_thread(client, thread)
    assert not leftovers, f"{len(leftovers)} of 10 start()/stop() pairs left a client thread running"
