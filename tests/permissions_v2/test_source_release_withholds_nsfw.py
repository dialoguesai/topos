"""p2a's source read never releases a message the owner's NSFW decision withholds.

`content_nsfw` withholds a row from every share (`disclosure/nsfw_tags.py`), and
every p2c-v3 family enforces it as a hard withhold (`message_evidence`,
`search_release`, `search_index`, `knowledge_projections`). The p2a locator door
releases each terminal message's whole raw `content`, and neither its resolver
floors (`evidence._eligible`) nor its release callback read the flag: under the
owner's implicit review a flagged message went out whole.

The door is the dedicated relay transport `release_transport.dispatch_source_message`
(off unless TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED=true), reached with a
CP-signed third_party stamp over a p2a-v3 grant, the one source capability the node
still serves. Rows are invented.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node
from tests.permissions_v2.test_bk3_opaque_ids import v3_policy
from tests.permissions_v2.test_release import dispatch
from tests.permissions_v2.test_release_transport import Socket
from topos.permissions_v2 import release_transport
from topos.permissions_v2.canonical import PolicyError
from topos.relay_stamp import canonical_signing_payload

pytestmark = pytest.mark.ordinal_ids_retired  # the node's real setting: only p2a-v3 releases


@pytest.fixture
def node(tmp_path):
    corpus = pc.build(tmp_path / "corpus", seed=47, positives=2)
    (corpus.path.parent / "permissions-v2").mkdir(mode=0o700)
    return Node(corpus, tmp_path, policy=v3_policy())


def flag(node, fact_id) -> str:
    """Set the NSFW decision on the fact's one cited message. Returns that message's text."""
    message_id = node.corpus.messages[fact_id]
    with sqlite3.connect(node.corpus.path) as conn:
        content = conn.execute("SELECT content FROM conversation_messages WHERE message_id=?", (message_id,)).fetchone()[0]
        conn.execute("UPDATE conversation_messages SET content_nsfw=1 WHERE message_id=?", (message_id,))
    return content


def implicit(node, fact_id) -> None:
    """Withdraw the owner's explicit review: the node's own labels apply, as for every unreviewed fact."""
    with pc.owner():
        current = node.corpus.reviews._load_current(fact_id)
        node.corpus.reviews.revoke_review(current.review_id, fact_id=fact_id)
        with pytest.raises(PolicyError, match="owner_review_required"):
            node.corpus.reviews._load_current(fact_id)  # no review is current: the implicit one applies


def text_of(node, fact_id) -> str:
    with sqlite3.connect(node.corpus.path) as conn:
        return conn.execute("SELECT content FROM conversation_messages WHERE message_id=?",
                            (node.corpus.messages[fact_id],)).fetchone()[0]


async def socket_read(node, fact_id, request_id, monkeypatch) -> list:
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(node.cp_key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(release_transport.time, "time", lambda: node.now[0])
    runtime = SimpleNamespace(protocol=node.protocol, evidence_reviews=lambda **kw: SimpleNamespace(
        resolver=node.corpus.resolver, reviews=node.corpus.reviews))
    monkeypatch.setattr(release_transport, "get_runtime", lambda: runtime)
    envelope, payload = node.issue(fact_id, request_id=request_id)
    message = {"id": request_id, "type": release_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope.model_dump(), "intent": payload}}
    now = node.now[0]
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1", "iat": now, "exp": now + 100}
    stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(
        stamp, msg_id=request_id, msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    socket = Socket()
    await release_transport.dispatch_source_message(socket, message)
    return socket.sent


DENIED = {"type": release_transport.MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize("review", ["implicit", "explicit"])
async def test_a_flagged_message_is_never_released(node, monkeypatch, review):
    control, fact = node.corpus.positives
    # The positive control: the door is open and releases an unflagged message whole.
    [frame] = await socket_read(node, control, "read-control", monkeypatch)
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"][0]["content"] == text_of(node, control)

    content = flag(node, fact)
    if review == "implicit":
        implicit(node, fact)
    else:
        # The owner reviews the message as it now stands, flag and all: a review is not a release of the flag.
        from topos.permissions_v2.evidence import ReviewedClassification
        with pc.owner():
            snapshot = node.corpus.resolver.inspect_for_review(fact)
            node.corpus.reviews.record_review(resolver=node.corpus.resolver, review_id="review-flagged",
                expected_snapshot=snapshot, reviewed_at=pc.NOW, classifications=[ReviewedClassification(
                    evidence=version, domains=["work"], sensitivity="none", subject_entity_ids=["self"],
                    authorship="owner_authored", speech="direct_self_statement", independent_copies="none_known")
                    for version in snapshot.artifacts + snapshot.leaves])
    frames = await socket_read(node, fact, "read-flagged", monkeypatch)
    assert frames == [{"id": "read-flagged", **DENIED}]
    assert content not in json.dumps(frames)


def test_the_withhold_is_the_nsfw_rule_not_another_floor(node):
    """The adapter the transport builds names why: the same reason p2c-v3 gives a flagged message."""
    _control, fact = node.corpus.positives
    flag(node, fact)
    implicit(node, fact)
    envelope, payload = node.issue(fact, request_id="read-reason")
    with pytest.raises(PolicyError, match="unsupported_message_content"):
        dispatch(node.setup, envelope, payload, request_id="read-reason")
