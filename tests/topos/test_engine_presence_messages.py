from __future__ import annotations

import asyncio
import time

import pytest

from topos.testing.lifespan import LifespanManager

# How long startup may take to send engine_register before this test calls it
# missing. The presence loop sends after its own 0.1s delay plus one thread hop
# (the capability probe runs off the loop), so a healthy send lands ~0.1s after
# startup and the send itself measured 5-14ms. The bound only has to sit far
# enough above that to mean "never sent" rather than "not yet", and well inside
# the 30s LifespanManager startup budget.
REGISTER_TIMEOUT_SECONDS = 10.0


async def _wait_for_message(sent: list[dict], message_type: str, timeout_s: float) -> bool:
    """Poll ``sent`` for a ``message_type`` message until ``timeout_s`` elapses.

    A fixed ``asyncio.sleep(0.25)`` used to stand here — 0.15s of slack over the
    presence loop's own delay — and it failed ONLY in the full public lane
    (5812 tests; 2 of 2 runs red on this test, 6 of 6 green alone, 2026-09-07).
    Nothing leaks into this test; the event loop is simply not responsive for
    0.15s late in that lane. A gen-2 garbage collection walks every tracked
    object in the process, and by test #5186 the suite's heap makes one pause
    165-254ms (three of them within 2s of this startup, each collecting 0
    objects; the lane's largest was 506ms) against 35-76ms for the same
    collections when the test runs alone: 951,845 tracked objects at this test
    in the lane against 115,584 alone. The collector holds the GIL, so the
    presence task's 0.1s timer and the fixed 0.25s sleep both expire under it
    and fire in the same loop iteration; the presence task then yields for its
    thread hop and the assertion runs first, before the send. Measured with
    gc.callbacks instrumentation on 1.3.54 (a74aab7); the one instrumented full
    run that passed did so with the send landing 1ms before the timer. Waiting
    on the message instead of the clock makes the assertion the one the test
    states: startup sends the message.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if any(msg.get("type") == message_type for msg in sent):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_startup_sends_engine_register_message(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from topos import app as app_module
    from topos.config.settings import settings
    import topos.core.state as state

    sent_messages: list[dict] = []

    class FakeControlPlaneClient:
        def __init__(self, control_plane_url: str, api_key: str, handler, verify_ssl: bool = True):
            self.control_plane_url = control_plane_url
            self.api_key = api_key
            self.handler = handler
            self.verify_ssl = verify_ssl
            self.started = False

        def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.started = False

        async def send_message(self, message):
            sent_messages.append(message)

    # Ensure startup path enables control plane client.
    monkeypatch.setattr(settings, "control_plane_url", "ws://example.test/ws/engine", raising=False)
    # Startup opens the configured DB (state + install_service); keep it off
    # the developer's live ~/.topos/database.db.
    monkeypatch.setattr(settings, "topos_database_path", str(tmp_path / "engine.db"))
    monkeypatch.setattr(app_module, "ControlPlaneClient", FakeControlPlaneClient, raising=True)

    async with LifespanManager(app_module.app):
        registered = await _wait_for_message(
            sent_messages, "engine_register", REGISTER_TIMEOUT_SECONDS
        )
        assert registered, (
            f"no engine_register within {REGISTER_TIMEOUT_SECONDS}s; "
            f"sent types={[m.get('type') for m in sent_messages]}"
        )

    # Ensure cleanup happens.
    assert state.engine_presence_task is None
