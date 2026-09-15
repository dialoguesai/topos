"""Exercise the real signed relay door and ws.send while evidence gates are held."""
import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_release import release_setup, issue
from tests.permissions_v2.test_evidence import corpus
from topos.permissions_v2 import release_transport
from topos.relay_stamp import canonical_signing_payload


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


@pytest.fixture
def relay(release_setup, monkeypatch):
    service, _, cp_key, _, now, _ = release_setup
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())
    monkeypatch.setattr(release_transport.time, "time", lambda: now[0])
    runtime = SimpleNamespace(protocol=service.protocol,
        evidence_reviews=lambda **kw: SimpleNamespace(resolver=service.resolver, reviews=service.reviews))
    monkeypatch.setattr(release_transport, "get_runtime", lambda: runtime)
    envelope, payload = issue(release_setup)
    message = {"id": "read-1", "type": release_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope.model_dump(), "intent": payload}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1", "iat": now[0], "exp": now[0] + 100}
    stamp["sig"] = base64.b64encode(cp_key.sign(canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    return release_setup, message


@pytest.mark.asyncio
async def test_actual_socket_receives_bound_message_not_generic_return_payload(relay):
    _, message = relay
    socket = Socket()
    assert await release_transport.dispatch_source_message(socket, message) is None
    assert len(socket.sent) == 1 and socket.sent[0]["status"] == "ok"
    assert socket.sent[0]["payload"]["output"]["records"][0]["content"] == "I enjoy reading history books."
    await release_transport.dispatch_source_message(socket, message)
    assert socket.sent[1]["status"] == "error" and "payload" not in socket.sent[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "missing_stamp", "bad_stamp", "changed_actor", "changed_client", "changed_query", "changed_id", "spoofed_owner"])
async def test_relay_door_denials_never_serialize_content(relay, monkeypatch, kind):
    _, message = relay
    if kind == "disabled":
        monkeypatch.delenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED")
    elif kind == "missing_stamp":
        del message["principal_stamp"]
    elif kind == "bad_stamp":
        message["principal_stamp"]["sig"] = "invalid"
    elif kind == "changed_actor":
        message["principal_stamp"]["acting_user"] = "owner-1"
    elif kind == "changed_client":
        message["principal_stamp"]["client_id"] = "owner-ui"
    elif kind == "changed_query":
        message["payload"]["intent"]["query"] = "fact:other"
    elif kind == "changed_id":
        message["id"] = "other-request"
    else:
        message["principal_stamp"]["cls"] = "owner_app"
        message["payload"]["mode"] = "owner"
    socket = Socket()
    await release_transport.dispatch_source_message(socket, message)
    assert socket.sent == [{"id": message["id"], "type": release_transport.MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}]


@pytest.mark.asyncio
async def test_generic_handler_never_returns_disclosure_even_with_valid_relay(relay):
    from topos.core.handlers import handle_control_plane_request
    from topos.relay_stamp import verify_relay_stamp
    _, message = relay
    response = await handle_control_plane_request(message, principal=verify_relay_stamp(message))
    assert response == {"id": "read-1", "status": "error", "code": 403, "error": "permission_denied"}


@pytest.mark.asyncio
async def test_socket_exception_is_content_free_and_cannot_queue_replay(relay):
    _, message = relay
    class Broken:
        async def send(self, value):
            raise RuntimeError("exception includes private transport input")
    await release_transport.dispatch_source_message(Broken(), message)
    socket = Socket()
    await release_transport.dispatch_source_message(socket, message)
    assert socket.sent[0]["status"] == "error"
    assert "private" not in json.dumps(socket.sent)
