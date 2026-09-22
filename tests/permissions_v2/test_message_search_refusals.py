"""One refusal for every grant-level failure, at the adapter and on the wire; and the transport's own doors.

The node adapter may raise different internal codes; the transport turns every one
into the same error frame, byte for byte. A refused request never sends a partial
answer. The transport sends only after the adapter returned, with no node gate held.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner, recipient
from topos.permissions_v2 import search_transport
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path, purge
from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
from topos.relay_stamp import canonical_signing_payload
from topos.storage.db import write_gate


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=13, counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": 4})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


PAYLOAD = {"query": "roadmap review", "k": 5}


def signed(node, *, grant_id=None, request_type="permissions.v2.search", payload=PAYLOAD, request_id="refuse-1",
           changes=None):
    grant_id = grant_id or node.search_raw["binding"]["grant_id"]
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    with owner():
        authority = node.ledger.authority_snapshot(grant_id, now=node.now[0])
    body = {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key", "request_id": request_id,
            "request_type": request_type, "request_hash": request_digest(request_type, payload),
            "issued_at": node.now[0], "expires_at": node.now[0] + 100, **(changes or {})}
    return sign_envelope(parse_envelope(body, signed=False), node.cp_key)


def search_with(node, envelope, payload=PAYLOAD, request_id="refuse-1"):
    with recipient():
        return node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)


# Each case prepares the node, then returns (envelope, payload, request_id) for one search.
def case_no_grant(node):
    envelope = signed(node)
    return sign_envelope(parse_envelope({**envelope.model_dump(exclude={"signature"}), "grant_id": "grant-missing"},
                                        signed=False), node.cp_key), PAYLOAD


def case_revoked(node):
    envelope = signed(node)
    with owner():
        node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-it")
    return envelope, PAYLOAD


def case_expired(node):
    envelope = signed(node)
    node.now[0] = mc.NOW + 8 * 86_400
    return envelope, PAYLOAD


def case_protection_changed(node):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','zz')")
    return signed(node), PAYLOAD


def case_black_hole(node):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO entity_blackholes(blackhole_id, normalized_name, canonical_name) VALUES('bh','x','X')")
    return signed(node), PAYLOAD


def case_over_k(node):
    node.activate({**mc.search_policy(max_k=3), "policy_version_id": "policy-small-k"}, generation=2)
    node.rebuild()
    payload = {"query": "roadmap", "k": 4}
    return signed(node, payload=payload), payload


def case_window_older_than_grant(node):
    payload = {"query": "roadmap", "k": 5, "window": {"after": mc.NOW - 200 * 86_400, "before": mc.NOW}}
    return signed(node, payload=payload), payload


def case_window_in_future(node):
    payload = {"query": "roadmap", "k": 5, "window": {"after": mc.NOW - 86_400, "before": mc.NOW + 86_400}}
    return signed(node, payload=payload), payload


def case_index_missing(node):
    purge(node.index.root, "grant-search")
    return signed(node), PAYLOAD


def case_index_stale(node):
    node.activate({**node.search_raw, "policy_version_id": "policy-2"}, generation=2)
    return signed(node), PAYLOAD


def case_over_cap(node):
    node.activate({**mc.search_policy(max_permitted=1), "policy_version_id": "policy-cap"}, generation=2)
    node.rebuild()
    return signed(node), PAYLOAD


def case_p2a_grant_on_search_door(node):
    return signed(node, grant_id="grant-p2a", request_type="permissions.v2.read",
                  payload={"query": "fact:" + node.corpus.units[0].fact_id}), PAYLOAD


def case_wrong_request_hash(node):
    return signed(node, payload={"query": "something else", "k": 5}), PAYLOAD


def case_bad_signature(node):
    envelope = signed(node).model_dump()
    envelope["signature"] = "A" * 86
    return parse_envelope(envelope), PAYLOAD


CASES = {name[5:]: value for name, value in globals().items() if name.startswith("case_")}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_grant_level_failure_is_a_refusal_with_no_output(node, case):
    envelope, payload = CASES[case](node)
    with pytest.raises(PolicyError):
        search_with(node, envelope, payload)


def test_replay_is_refused(node):
    envelope = signed(node)
    search_with(node, envelope)
    with pytest.raises(PolicyError, match="request_replay"):
        search_with(node, envelope)


def test_p2c_grant_on_the_locator_door_is_refused(node):
    unit = next(unit for unit in node.corpus.units if unit.search_release)
    payload = {"query": "fact:" + unit.fact_id}
    envelope = signed(node, request_type="permissions.v2.search", payload=payload)
    with recipient():
        with pytest.raises(PolicyError):
            node.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="refuse-1",
                                  send=lambda *_: pytest.fail("sent"))


def test_refusals_after_admission_leave_one_deny_receipt(node):
    envelope, payload = case_window_older_than_grant(node)
    with pytest.raises(PolicyError, match="permission_denied"):
        search_with(node, envelope, payload)
    with sqlite3.connect(node.ledger.path) as conn:
        receipt, decision = conn.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id='refuse-1'").fetchone()
    receipt, decision = json.loads(receipt), json.loads(decision)
    assert receipt["version"] == "topos-local-receipt/v3" and receipt["verdict"] == "deny"
    assert receipt["output_hash"] is None and receipt["record_count"] == 0
    assert decision["reason_code"] == "set_refused"


# -- the wire ------------------------------------------------------------------------

class Socket:
    def __init__(self, on_send=None):
        self.sent, self.on_send = [], on_send

    async def send(self, value):
        if self.on_send:
            self.on_send()
        self.sent.append(value)


def relay_message(node, envelope, payload, monkeypatch, request_id="refuse-1"):
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(node.cp_key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(search_transport.time, "time", lambda: node.now[0])
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    monkeypatch.setattr(search_transport, "get_runtime", lambda: runtime)
    message = {"id": request_id, "type": search_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope.model_dump(), "intent": payload}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-2", "acting_user": "actor-1", "iat": node.now[0],
             "exp": node.now[0] + 100}
    stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(stamp, msg_id=message["id"],
                                                                                 msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    return message


REFUSAL = json.dumps({"code": 403, "error": "permission_denied", "id": "refuse-1", "status": "error",
                      "type": "permissions_v2_message_search"}, separators=(",", ":"), sort_keys=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_every_refusal_class_is_the_same_bytes_on_the_wire(node, monkeypatch, case):
    envelope, payload = CASES[case](node)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, relay_message(node, envelope, payload, monkeypatch))
    assert socket.sent == [REFUSAL]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["disabled", "missing_stamp", "bad_stamp", "owner_stamp", "changed_id", "extra_key",
                                  "changed_query"])
async def test_transport_doors_refuse_with_the_same_bytes(node, monkeypatch, door):
    message = relay_message(node, signed(node), PAYLOAD, monkeypatch)
    if door == "disabled":
        monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED")
    elif door == "missing_stamp":
        del message["principal_stamp"]
    elif door == "bad_stamp":
        message["principal_stamp"]["sig"] = "invalid"
    elif door == "owner_stamp":
        message["principal_stamp"]["cls"] = "owner_app"
    elif door == "changed_id":
        message["id"] = "refuse-1"
        message["payload"]["envelope"]["request_id"] = "other"
    elif door == "extra_key":
        message["payload"]["mode"] = "owner"
    else:
        message["payload"]["intent"] = {"query": "other", "k": 5}
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    assert socket.sent == [REFUSAL]


@pytest.mark.asyncio
async def test_answer_frame_is_closed_and_sent_with_no_gate_held(node, monkeypatch):
    message = relay_message(node, signed(node), PAYLOAD, monkeypatch)
    held = []

    def probe():
        # Another thread must be able to take the node write gate while the send runs.
        got = []
        def take():
            got.append(write_gate._WRITE_LOCK.acquire(timeout=1))
            if got[0]:
                write_gate._WRITE_LOCK.release()
        thread = threading.Thread(target=take)
        thread.start()
        thread.join()
        held.append(not got[0])
    socket = Socket(on_send=probe)
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    assert held == [False]
    assert set(frame) == {"id", "type", "status", "payload"} and frame["status"] == "ok"
    assert set(frame["payload"]) == {"result", "output"}
    assert set(frame["payload"]["output"]) == {"family", "operation", "view_id", "records"}
    assert frame["payload"]["output"]["records"]


@pytest.mark.asyncio
async def test_generic_handler_never_returns_a_search_payload(node, monkeypatch):
    from topos.core.handlers import handle_control_plane_request
    from topos.relay_stamp import verify_relay_stamp
    message = relay_message(node, signed(node), PAYLOAD, monkeypatch)
    response = await handle_control_plane_request(message, principal=verify_relay_stamp(message))
    assert response == {"id": "refuse-1", "status": "error", "code": 403, "error": "permission_denied"}
