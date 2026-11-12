"""A slow standard verifier must not hold the app event loop or share its DB handle.

No live data or inference: the synchronous verifier waits for a callback on the
event loop. Before the fix that callback cannot run until the wait expires.
"""
import asyncio
import logging
import threading

import pytest

import topos.core.handlers as hub
from topos.core.handlers import query
from topos.features.lifecycle import blackhole_guard
from topos.principal import OWNER_APP, Principal, current_principal, reset_principal, set_principal
from topos.query import verify


@pytest.mark.parametrize("mode", [None, "fun"])
def test_slow_verification_keeps_loop_responsive_with_worker_owned_guard(monkeypatch, mode, caplog):
    caplog.set_level(logging.INFO, logger="topos.core.handlers")
    seen = {}
    connection, guard = object(), object()
    owner = Principal(cls=OWNER_APP, channel="uds")

    def get_connection():
        seen["connection_thread"] = threading.get_ident()
        return connection

    def make_guard(conn, message):
        assert conn is connection
        seen["guard_thread"] = threading.get_ident()
        return guard

    def record_usage(app, result):
        seen["usage_thread"] = threading.get_ident()
        seen["usage"] = (app, result)

    monkeypatch.setattr(hub, "get_db_connection", get_connection)
    monkeypatch.setattr(blackhole_guard, "guard_from_message", make_guard)
    monkeypatch.setattr(query, "_record_verify_claim_after_answer", record_usage)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        released = threading.Event()

        def slow_verifier(conn, **kwargs):
            seen["verify_thread"] = threading.get_ident()
            assert conn is connection and kwargs["guard"] is guard
            assert current_principal() == owner
            assert kwargs["mode"] == (mode or "standard")
            loop.call_soon_threadsafe(released.set)
            seen["loop_responded_while_waiting"] = released.wait(1.0)
            return {"mode": kwargs["mode"], "lanes": {}, "_audit": {"judge": "synthetic"}}

        monkeypatch.setattr(verify, "verify_claim", slow_verifier)
        payload = {"statement": "Invented statement", "caller_app_id": "test"}
        if mode:
            payload["mode"] = mode
        token = set_principal(owner)
        try:
            response = await query.handle_verify_claim({"id": "test-r", "payload": payload})
        finally:
            reset_principal(token)
        assert response == {"id": "test-r", "status": "ok", "payload": {"mode": mode or "standard", "lanes": {}}}
        assert seen["loop_responded_while_waiting"], "synchronous verification blocked the event loop"
        assert seen["connection_thread"] == seen["guard_thread"] == seen["verify_thread"] != loop_thread
        assert seen["usage_thread"] == loop_thread
        assert seen["usage"] == ("test", response["payload"])
        logged = next(r.args for r in caplog.records if r.msg == "truth verify_claim audit: %s")
        timings = logged["timings_ms"]
        assert timings["worker_run"] >= 0
        assert timings["worker_ready"] >= 0
        assert timings["worker_return_to_loop"] >= 0
        assert abs(timings["dispatch_to_result"] - sum(timings[k] for k in ("worker_ready", "worker_run", "worker_return_to_loop"))) <= .003

    asyncio.run(exercise())
