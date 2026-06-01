from __future__ import annotations

import asyncio

import pytest

from topos.testing.lifespan import LifespanManager


@pytest.mark.asyncio
async def test_startup_sends_engine_register_message(monkeypatch: pytest.MonkeyPatch):
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
    monkeypatch.setattr(app_module, "ControlPlaneClient", FakeControlPlaneClient, raising=True)

    async with LifespanManager(app_module.app):
        await asyncio.sleep(0.25)
        assert any(msg.get("type") == "engine_register" for msg in sent_messages)

    # Ensure cleanup happens.
    assert state.engine_presence_task is None
