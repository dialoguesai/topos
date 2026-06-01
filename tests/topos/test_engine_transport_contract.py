from __future__ import annotations

import json

import pytest

from topos.engine.transport import (
    dispatch_compute_message,
    handle_endpoint_request,
    handle_ws_raw_message,
    normalize_transport_mode,
)


def test_normalize_transport_mode_defaults_to_ws():
    assert normalize_transport_mode(None) == "ws"
    assert normalize_transport_mode("") == "ws"
    assert normalize_transport_mode("invalid") == "ws"
    assert normalize_transport_mode("endpoint") == "endpoint"


@pytest.mark.asyncio
async def test_dispatch_compute_message_ws_mode():
    async def handler(payload):
        return {"ok": True, "echo": payload}

    out = await dispatch_compute_message(mode="ws", payload={"x": 1}, handler=handler)
    assert out == {"ok": True, "echo": {"x": 1}}


@pytest.mark.asyncio
async def test_handle_ws_raw_message_roundtrip():
    async def handler(payload):
        return {"id": payload.get("id"), "status": "ok"}

    raw = json.dumps({"id": "abc", "type": "ping"})
    out = await handle_ws_raw_message(raw, handler)
    assert out is not None
    parsed = json.loads(out)
    assert parsed["id"] == "abc"
    assert parsed["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_endpoint_request_uses_handler():
    async def handler(payload):
        return {"status": "ok", "payload": payload}

    out = await handle_endpoint_request({"job": "1"}, handler)
    assert out == {"status": "ok", "payload": {"job": "1"}}
