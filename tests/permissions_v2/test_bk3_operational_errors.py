"""F3/F5 (node): an operational error leaves the node as the one uniform refusal frame.

A locked or busy SQLite file at the ledger, the canonical read or the review store
must not surface as anything a recipient can tell apart from a policy refusal. The
transports already turn every exception into the one frame; these tests pin that for
the operational errors the boundary catalog names (F3), through the real WebSocket
dispatch, against the frame a nonexistent fact produces.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node
from tests.permissions_v2.test_release_transport import Socket
from topos.permissions_v2 import evidence, ledger, release_transport
from topos.relay_stamp import canonical_signing_payload


@pytest.fixture
def relay(tmp_path, monkeypatch):
    corpus = pc.build(tmp_path / "corpus", seed=51, positives=1)
    node = Node(corpus, tmp_path)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(node.cp_key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(release_transport.time, "time", lambda: node.now[0])
    runtime = SimpleNamespace(protocol=node.protocol,
        evidence_reviews=lambda **kw: SimpleNamespace(resolver=corpus.resolver, reviews=corpus.reviews))
    monkeypatch.setattr(release_transport, "get_runtime", lambda: runtime)

    def message(fact_id, request_id):
        envelope, payload = node.issue(fact_id, request_id=request_id)
        body = {"id": request_id, "type": release_transport.MESSAGE_TYPE,
                "payload": {"envelope": envelope.model_dump(), "intent": payload}}
        stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1",
                 "iat": node.now[0], "exp": node.now[0] + 100}
        stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(
            stamp, msg_id=body["id"], msg_type=body["type"]))).decode()
        body["principal_stamp"] = stamp
        return body
    return node, message


async def frame(message):
    socket = Socket()
    await release_transport.dispatch_source_message(socket, message)
    [sent] = socket.sent
    return {key: value for key, value in sent.items() if key != "id"}


def locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["ledger", "canonical_read", "review_store"])
async def test_a_locked_store_refuses_exactly_as_a_missing_fact_does(relay, monkeypatch, where):
    node, message = relay
    expected = await frame(message("no-such-fact", "baseline"))
    assert expected == {"type": release_transport.MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}
    probe = message(node.corpus.positives[0], "probe")
    if where == "ledger":
        monkeypatch.setattr(ledger.PolicyLedger, "_transaction", locked)
    elif where == "canonical_read":
        real = evidence.sqlite3.connect
        monkeypatch.setattr(evidence.sqlite3, "connect",
                            lambda target, *a, **k: locked() if "mode=ro" in str(target) else real(target, *a, **k))
    else:
        monkeypatch.setattr(evidence.EvidenceReviewStore, "_db", locked)
    assert await frame(probe) == expected
    assert "locked" not in json.dumps(expected)
