"""Fuzz lane, part 4: refusal is uniform at the node's recipient door, for every failure class.

U1  Whatever the adapter raises -- any PolicyError code, any other exception type -- the socket
    receives exactly one frame, and that frame is the one refusal frame of the door: `{id, type,
    status: "error", code: 403, error: "permission_denied"}`, with nothing of the exception in it.
U2  Any malformed relayed message -- a wrong type, a payload missing a key, an envelope of
    another capability, an unstamped or mis-stamped frame -- yields the same single frame,
    before the runtime is touched.
U4  A success frame is never emitted on a refusal path: no `status: "ok"` and no payload key.

The door is knowledge search (p2c-v3), the one recipient door the node has. The locator and fact
doors ran through the same three properties, and a fourth (U3: the doors' frames differ only in
the message type), until N8 removed them. The envelope names p2c-v3: under a retired profile the
transport's own parser refuses first and U1 would never reach the adapter.

The stamp is a real CP relay stamp under a pinned key, so the refusal is raised inside the
adapter, past the relay door.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("hypothesis")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from hypothesis import given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2.test_recipient_fabric_refusal_uniformity import refusal, sign_stamp  # noqa: E402
from topos.permissions_v2 import search_transport  # noqa: E402
from topos.permissions_v2.canonical import PolicyError, canonical_bytes  # noqa: E402
from topos.permissions_v2.signing import EnvelopeBody, KnowledgeEnvelopeBody, request_digest, sign_envelope  # noqa: E402

pytestmark = [pytest.mark.fuzz]
PURE = settings(max_examples=fz.examples("pure"))
CP_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(7, 39)))
NOW = 1_800_000_000
DOORS = {
    "search": dict(module=search_transport, dispatch=search_transport.dispatch_message_search,
                   flag=search_transport.FLAG, body=KnowledgeEnvelopeBody,
                   request_type="permissions.v2.search", capability="permissions-beta/p2c-v3"),
}
#: An envelope the search door must never take for its own: a historical locator read.
FOREIGN = dict(body=EnvelopeBody, request_type="permissions.v2.read", capability="permissions-beta/p2a-v1")
CODES = ["permission_denied", "evidence_missing", "owner_only", "review_stale", "grant_inactive", "policy_time",
         "authority_stale", "disclosure_budget", "request_replay", "unsupported_capability", "record_key_unavailable",
         "entity_protection_lineage_unavailable", "search_index_missing"]
EXCEPTIONS = [ValueError("a value with a synthetic sentence in it"), KeyError("k"), TypeError("t"),
              RuntimeError("SYNTHETIC-DIAGNOSTIC"), sqlite3.OperationalError("database is locked"),
              ZeroDivisionError(), AssertionError("assert"), RecursionError(), LookupError(),
              OSError(24, "Too many open files"), MemoryError(), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x")]


@pytest.fixture(autouse=True, scope="module")
def pinned_stamp_key():
    saved = {name: os.environ.get(name) for name in ("TOPOS_CP_STAMP_PUBKEY", *[d["flag"] for d in DOORS.values()])}
    os.environ["TOPOS_CP_STAMP_PUBKEY"] = base64.b64encode(
        CP_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    for door in DOORS.values():
        os.environ[door["flag"]] = "true"
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def envelope_for(door, request_id, payload, spec=None):
    spec = spec or DOORS[door]
    body = spec["body"].parse({
        "environment_id": "permissions-beta-fuzz", "node_id": "node-1", "resource_id": "resource-1",
        "owner_id": "owner-1", "actor_id": "actor-1", "client_id": "client-1", "grant_id": "grant-1",
        "assignment_id": "assignment-1", "grant_generation": 1, "assignment_generation": 1,
        "policy_version_id": "policy-1", "policy_hash": "a" * 64, "capability_version": spec["capability"],
        "protection_revision": "b" * 64, "node_epoch": 1, "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": request_id, "request_type": spec["request_type"],
        "request_hash": request_digest(spec["request_type"], payload), "issued_at": NOW, "expires_at": NOW + 100})
    return sign_envelope(body, CP_KEY).model_dump()


def message_for(door, request_id="req-1"):
    payload = {"query": "roadmap", "k": 5}
    message = {"id": request_id, "type": DOORS[door]["module"].MESSAGE_TYPE,
               "payload": {"envelope": envelope_for(door, request_id, payload), "intent": payload}}
    # Stamped at the clock the door is run under: a stamp from the wall clock is expired there, the relay
    # door refuses it, and nothing below would ever reach the adapter.
    return sign_stamp(message, CP_KEY, iat=NOW, exp=NOW + 100)


class Raising:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def dispatch(self, **kwargs):
        self.calls += 1
        raise self.error


def run(door, message, error, monkeypatch):
    """Dispatch one relayed message through the real transport with the adapter replaced by `error`."""
    spec = DOORS[door]
    frames = []

    async def send(value):
        frames.append(value)

    with monkeypatch.context() as patch:
        patch.setattr(spec["module"].time, "time", lambda: NOW + 1)
        adapter = Raising(error)
        runtime = SimpleNamespace(message_search=lambda: adapter, protocol=None)
        patch.setattr(spec["module"], "get_runtime", lambda: runtime)
        asyncio.run(spec["dispatch"](SimpleNamespace(send=send), message))
    # The refusal under test is the adapter's: a door that refused earlier would prove nothing about it.
    assert adapter.calls == 1
    return frames


def errors():
    return st.one_of(st.sampled_from(CODES).map(PolicyError), st.text(min_size=1, max_size=40).map(PolicyError),
                     st.sampled_from(EXCEPTIONS))


@PURE
@given(st.sampled_from(sorted(DOORS)), errors(), fz.identifiers)
def test_U1_whatever_the_adapter_raises_the_door_emits_the_one_refusal_frame(monkeypatch, door, error, request_id):
    frames = run(door, message_for(door, request_id), error, monkeypatch)
    assert frames == [refusal(request_id, DOORS[door]["module"].MESSAGE_TYPE)]
    # Byte equality with the constant frame is the whole proof: no code, message or type of `error` is in it.
    assert "payload" not in json.loads(frames[0]) and json.loads(frames[0])["status"] == "error"


def malformed():
    return st.sampled_from(["wrong_type", "missing_intent", "missing_envelope", "extra_key", "payload_list",
                            "no_payload", "other_door_envelope", "id_mismatch", "no_stamp", "tampered_stamp",
                            "owner_stamp", "expired_stamp"])


def malform(door, message, how):
    stamp = dict(message["principal_stamp"])
    if how == "wrong_type":
        message["type"] = "permissions_v2_unknown"
    elif how == "missing_intent":
        del message["payload"]["intent"]
    elif how == "missing_envelope":
        del message["payload"]["envelope"]
    elif how == "extra_key":
        message["payload"]["extra"] = 1
    elif how == "payload_list":
        message["payload"] = [message["payload"]]
    elif how == "no_payload":
        del message["payload"]
    elif how == "other_door_envelope":
        message["payload"]["envelope"] = envelope_for(door, message["id"], {"query": "fact:synthetic-fact"}, FOREIGN)
    elif how == "id_mismatch":
        message["id"] = message["id"] + "-other"
    elif how == "no_stamp":
        del message["principal_stamp"]
    elif how == "tampered_stamp":
        stamp["acting_user"] = "actor-2"
        message["principal_stamp"] = stamp
    elif how == "owner_stamp":
        sign_stamp(message, CP_KEY, cls="owner_app", acting_user="owner-1", client_id="topos_home_chat")
    elif how == "expired_stamp":
        sign_stamp(message, CP_KEY, iat=NOW - 700, exp=NOW - 1)
    return message


@PURE
@given(st.sampled_from(sorted(DOORS)), malformed(), fz.identifiers)
def test_U2_a_malformed_relayed_message_is_the_same_frame_before_the_runtime_is_touched(monkeypatch, door, how, request_id):
    message = malform(door, message_for(door, request_id), how)
    touched = []

    def runtime():
        touched.append(True)
        raise AssertionError("the runtime was reached")

    frames = []

    async def send(value):
        frames.append(value)

    spec = DOORS[door]
    with monkeypatch.context() as patch:
        patch.setattr(spec["module"].time, "time", lambda: NOW + 1)
        patch.setattr(spec["module"], "get_runtime", runtime)
        asyncio.run(spec["dispatch"](SimpleNamespace(send=send), message))
    assert frames == [refusal(message.get("id"), spec["module"].MESSAGE_TYPE)]
    assert touched == []
