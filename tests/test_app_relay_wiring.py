"""Review R1 (node), R-M8: the app hands relay frames to the dispatcher that applies the rules, and runs the pin thread.

protects: both relay rules (a caller who is not the owner reaches only the share doors; a stamp that does not verify
is refused) live in ``core.handlers.dispatch_relay_message``, and the retry of the first stamp-key pin lives in
``relay_stamp.start_first_pin``. Whether a node applies any of it is decided by a few lines of ``topos/app.py``
that no test ran: with ``_relay_dispatch`` rewritten to call ``handle_control_plane_request`` with the relay
deferral (the handoff before the rules), or with ``start_first_pin()`` or ``stop_first_pin()`` removed, every nearby
suite stayed green (the reviewer's mutant: 388 passed each time).

This test starts the real app with a control plane address, takes the handler the app really gives its control-plane
client, and sends it frames. No network: the client is a recorder, and the one request the pin thread makes is
answered by a recorder that cannot be reached. Keys are made at run time; every id is invented.
"""
from __future__ import annotations

import base64
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_topos_home_hermeticity import _fresh_owner_state, scratch_home  # noqa: F401 (fixture)

KEY = Ed25519PrivateKey.generate()
ANOTHER_KEY = Ed25519PrivateKey.generate()
OWNER, SOMEONE_ELSE = "owner-1", "someone-else"


def stamped(message, *, cls, acting, key=KEY):
    from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload

    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "some-client", "acting_user": acting, "iat": now, "exp": now + 100}
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


def pin_threads():
    from topos import relay_stamp

    return [thread for thread in threading.enumerate() if thread.name == relay_stamp.FIRST_PIN_THREAD and thread.is_alive()]


@pytest.mark.asyncio
async def test_the_app_gives_its_client_the_dispatcher_and_keeps_the_pin_thread(scratch_home, tmp_path, monkeypatch):  # noqa: F811
    import httpx

    from topos import app as app_module
    from topos import relay_stamp
    from topos.config.settings import settings
    from topos.core.handlers.registry import HANDLERS
    from topos.principal import OWNER_APP, THIRD_PARTY
    from topos.testing.lifespan import LifespanManager

    given, asked, reached = {}, [], []

    class Client:
        """Stands in for ControlPlaneClient: it keeps what the app hands it and connects to nothing."""

        def __init__(self, control_plane_url, api_key, handler, verify_ssl=True):
            given["handler"] = handler

        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def send_message(self, message) -> None:
            pass

        def set_device_info_snapshot(self, payload) -> None:
            pass

    def unreachable(url, **_options):
        asked.append(url)
        raise ConnectionError("the control plane cannot be reached")

    async def recorder(message):
        reached.append(message["id"])
        return {"id": message["id"], "status": "ok", "payload": {}}

    _fresh_owner_state()
    monkeypatch.setattr(settings, "control_plane_url", "wss://cp.example/ws/engine", raising=False)
    monkeypatch.setattr(settings, "topos_database_path", str(tmp_path / "engine.db"))
    monkeypatch.setattr(app_module, "ControlPlaneClient", Client)
    monkeypatch.setattr(relay_stamp, "_PINNED_KEY_PATH", str(tmp_path / "pin" / "cp_stamp_key.pub"))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    monkeypatch.setattr(httpx, "get", unreachable)                    # the pin thread's one kind of request
    monkeypatch.setitem(HANDLERS, "query", recorder)
    refused = {"status": "error", "code": 403, "error": "owner_mode_required"}
    assert pin_threads() == []

    async with LifespanManager(app_module.app):
        relay = given["handler"]                                      # what the app really gave its client
        # The node holds no key and its control plane cannot be reached: the pin thread is alive and trying.
        for _ in range(100):
            if asked:
                break
            time.sleep(0.02)
        assert asked == ["https://cp.example/v1/relay/stamp-public-key"]
        assert len(pin_threads()) == 1
        # ... and while it holds none, a stamped frame is refused, never read as "no stamp".
        assert await relay(stamped({"id": "f0", "type": "query", "payload": {}}, cls=OWNER_APP, acting=OWNER)) == {
            "id": "f0", **refused}
        # Now it holds the control plane's key.
        monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(KEY.public_key().public_bytes_raw()).decode())
        frame = {"type": "query", "payload": {}}
        # A verified third party who is not this node's owner: only the share doors.
        assert await relay(stamped({"id": "f1", **frame}, cls=THIRD_PARTY, acting=SOMEONE_ELSE)) == {"id": "f1", **refused}
        # A stamp that does not verify (another key's): refused, not the relay deferral.
        assert await relay(stamped({"id": "f2", **frame}, cls=OWNER_APP, acting=OWNER, key=ANOTHER_KEY)) == {
            "id": "f2", **refused}
        assert reached == []
        # The controls: the owner's own app, and a frame with no stamp, reach the handler through the same door.
        assert (await relay(stamped({"id": "f3", **frame}, cls=OWNER_APP, acting=OWNER)))["status"] == "ok"
        assert (await relay({"id": "f4", **frame}))["status"] == "ok"
        assert reached == ["f3", "f4"]

    # Shutdown ended the tries at once: the thread is gone, not waiting out its next ten minutes.
    for _ in range(250):
        if not pin_threads():
            break
        time.sleep(0.02)
    assert pin_threads() == []
