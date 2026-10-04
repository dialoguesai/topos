"""N3 (decision D13): the signed daily limit holds on the node.

`read_budget_per_day` is signed into every share's policy. The node counts one question per share per UTC day where it
claims the request id for a release, in the ledger's own transaction, and refuses past the limit with the uniform
refusal: the id is spent, nothing is released. One search, one batch whatever its size (A2A-5 Q3) or one ask is one
question. A refused read is not counted. The count is stored in the ledger, so it survives a restart, and days older
than eight are pruned. Every person, message and number here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner, recipient
from tests.permissions_v2.test_message_search_batch import batch_error, ledger_rows, send_batch, spent_with_refusal
from tests.permissions_v2.test_message_search_refusals import REFUSAL, Socket, relay_message, signed
from topos.permissions_v2 import search_transport
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.ledger import (QUESTION_DAY_RETENTION_DAYS, NodeIdentity, PolicyLedger, utc_day)
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.search_release import MessageSearchRelease

GRANT = "grant-search"
QUERIES = [{"query": "roadmap review", "k": 5}, {"query": "roadmap", "k": 3}, {"query": "budget sprint", "k": 5},
           {"query": "launch deploy", "k": 2}, {"query": "vendor contract", "k": 5}, {"query": "review", "k": 4}]
# The end of the corpus clock's UTC day (2027-01-15T23:59:59Z).
END_OF_DAY = mc.NOW - mc.NOW % 86_400 + 86_399


def budgeted(budget, **changes):
    return {**mc.search_policy(), "read_budget_per_day": budget, **changes}


@pytest.fixture
def corpus(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=21, counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": 6})
    embed_corpus(corpus)
    return corpus


def node_with(corpus, tmp_path, policy):
    node = Node(corpus, tmp_path / "node", search_raw=policy)
    node.rebuild()
    return node


def asked(node, grant_id=GRANT) -> int:
    return node.ledger.questions_asked(grant_id, now=node.now[0])


def search(node, query="roadmap review", **options):
    return node.search_request(query, k=5, **options)


# -- one search, one question ------------------------------------------------------------------------------------

def test_the_search_after_the_limit_is_refused_its_id_spent_and_not_counted(corpus, tmp_path):
    node = node_with(corpus, tmp_path, budgeted(2))
    assert [search(node)[1], search(node)[1]] == [None, None]
    assert asked(node) == 2
    output, reason = search(node, request_id="over-the-limit")
    assert output is None and reason == "permission_denied"
    assert asked(node) == 2                                    # the refusal is not a question
    assert spent_with_refusal(node, "over-the-limit")          # the id is spent, with the one deny receipt
    output, reason = search(node, request_id="over-the-limit")  # and stays spent
    assert output is None and reason == "request_replay"


def test_an_undeclared_limit_counts_every_question_and_refuses_none(corpus, tmp_path):
    node = node_with(corpus, tmp_path, mc.search_policy())
    for _ in range(5):
        output, reason = search(node)
        assert reason is None and output["records"]
    assert asked(node) == 5


def test_a_refused_search_is_not_a_question(corpus, tmp_path):
    node = node_with(corpus, tmp_path, {**mc.search_policy(max_k=3), "read_budget_per_day": 1})
    output, reason = node.search_request("roadmap", k=4, request_id="above-k")   # verified, then refused (k)
    assert output is None and reason == "permission_denied"
    assert spent_with_refusal(node, "above-k")
    assert asked(node) == 0
    output, reason = node.search_request("roadmap", k=3)
    assert reason is None and asked(node) == 1


def test_a_limit_signed_later_the_same_day_counts_the_questions_already_asked(corpus, tmp_path):
    node = node_with(corpus, tmp_path, mc.search_policy())
    assert [search(node)[1], search(node)[1]] == [None, None]
    node.activate(budgeted(2, policy_version_id="policy-with-a-limit"), generation=2)
    node.rebuild()
    output, reason = search(node)
    assert output is None and reason == "permission_denied"
    assert asked(node) == 2


def test_the_day_is_the_utc_day(corpus, tmp_path):
    node = node_with(corpus, tmp_path, budgeted(1))
    node.now[0] = END_OF_DAY
    assert search(node)[1] is None
    assert search(node)[0] is None                              # the same UTC day: refused
    node.now[0] = END_OF_DAY + 1                                # 00:00:00Z the next day
    output, reason = search(node)
    assert reason is None and output["records"]
    assert asked(node) == 1
    assert (utc_day(END_OF_DAY), utc_day(END_OF_DAY + 1)) == ("2027-01-15", "2027-01-16")


# -- a batch, one question -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_batch_of_any_size_is_one_question_and_past_the_limit_is_refused_whole(corpus, tmp_path, monkeypatch):
    node = node_with(corpus, tmp_path, budgeted(2))
    frame = await send_batch(node, QUERIES, monkeypatch, batch_id="batch-six")
    assert frame["status"] == "ok" and len(frame["payload"]["items"]) == 6
    assert asked(node) == 1                                     # six queries, one question
    frame = await send_batch(node, QUERIES[:3], monkeypatch, batch_id="batch-three")
    assert frame["status"] == "ok" and asked(node) == 2
    frame = await send_batch(node, QUERIES[:4], monkeypatch, batch_id="batch-over")
    assert json.dumps(frame, separators=(",", ":"), sort_keys=True) == batch_error("batch-over")
    assert asked(node) == 2
    assert all(spent_with_refusal(node, f"batch-over:{number}") for number in range(4))


# -- the uniform refusal on the wire -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_past_the_limit_the_wire_carries_the_one_refusal_bytes(corpus, tmp_path, monkeypatch):
    node = node_with(corpus, tmp_path, budgeted(1))
    assert search(node)[1] is None
    socket = Socket()
    await search_transport.dispatch_message_search(
        socket, relay_message(node, signed(node), {"query": "roadmap review", "k": 5}, monkeypatch))
    assert socket.sent == [REFUSAL]


# -- across a restart ----------------------------------------------------------------------------------------------

def reopened(node) -> MessageSearchRelease:
    """The node's ledger, protocol and search door opened again from their files, as a restarted process does."""
    resolver = node.corpus.resolver
    with node.ledger._transaction() as conn:
        floor = node.ledger._node(conn)["protection_revision"]
    ledger = PolicyLedger(node.ledger.path, identity=NodeIdentity.parse(resolver.binding.model_dump()),
                          protection_revision=floor, trusted_keys=node.cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=resolver.path, cp_issuer_id="cp-issuer",
                                  frontend_client_id="owner-ui", trusted_cp_keys=node.cp_keys,
                                  node_signing_kid="node-key", node_signing_key=node.node_key)
    node.ledger, node.protocol = ledger, protocol
    node.search = MessageSearchRelease(protocol=protocol, resolver=resolver, reviews=node.corpus.reviews,
                                       index=node.index, clock=lambda: node.now[0], embedder=node.search.embedder)
    node.index.ledger = ledger
    return node.search


def test_the_count_survives_a_restart(corpus, tmp_path):
    node = node_with(corpus, tmp_path, budgeted(2))
    assert [search(node)[1], search(node)[1]] == [None, None]
    reopened(node)
    assert asked(node) == 2
    output, reason = search(node)
    assert output is None and reason == "permission_denied"


# -- every door that claims for a release ----------------------------------------------------------------------------

def test_the_locator_door_keeps_the_same_limit(corpus, tmp_path):
    node = node_with(corpus, tmp_path, mc.search_policy())
    node.activate({**mc.p2a_v2_policy(), "read_budget_per_day": 1, "policy_version_id": "policy-p2a-limited"},
                  generation=2)
    unit = next(unit for unit in corpus.units if unit.search_release)
    assert node.locator_read(unit.fact_id) is not None
    assert node.locator_read(unit.fact_id) is None
    assert asked(node, "grant-p2a") == 1


# -- pruned after eight days ----------------------------------------------------------------------------------------

def test_days_older_than_eight_are_pruned_at_a_later_claim(corpus, tmp_path):
    node = node_with(corpus, tmp_path, budgeted(5))
    kept, gone = utc_day(node.now[0] - QUESTION_DAY_RETENTION_DAYS * 86_400), utc_day(
        node.now[0] - (QUESTION_DAY_RETENTION_DAYS + 1) * 86_400)
    with sqlite3.connect(node.ledger.path) as conn:
        conn.executemany("INSERT INTO p2a_question_days VALUES (?,?,?)",
                         [(GRANT, kept, 3), (GRANT, gone, 4), ("grant-elsewhere", gone, 1)])
    assert node.ledger.question_counts(now=node.now[0]) == {GRANT: 3}
    assert search(node)[1] is None
    with sqlite3.connect(node.ledger.path) as conn:
        days = sorted(conn.execute("SELECT grant_id, utc_day, questions FROM p2a_question_days").fetchall())
    assert days == [(GRANT, kept, 3), (GRANT, utc_day(node.now[0]), 1)]


def test_the_ledger_counts_one_question_per_grant_in_the_claiming_transaction(corpus, tmp_path, monkeypatch):
    """A claim that fails after the count leaves no count: the count is inside the claim's own transaction."""
    node = node_with(corpus, tmp_path, budgeted(3))
    envelope = signed(node, request_id="rolled-back")
    with recipient():
        admission = node.ledger.verify(envelope.model_dump(), request=_request(node, envelope),
                                       payload={"k": 5, "query": "roadmap review"}, now=node.now[0])
    real = PolicyLedger._count_question

    def count_then_fail(self, conn, envelopes, *, now):
        real(self, conn, envelopes, now=now)
        raise PolicyError("injected")
    with monkeypatch.context() as patched:
        patched.setattr(PolicyLedger, "_count_question", count_then_fail)
        with pytest.raises(PolicyError, match="injected"):
            node.ledger.admit_verified(admission, now=node.now[0])
    assert asked(node) == 0 and ledger_rows(node, "rolled-back") == (None, None)
    node.ledger.admit_verified(admission, now=node.now[0])
    assert asked(node) == 1


def _request(node, envelope):
    from topos.permissions_v2.signing import SearchRequestContext
    return SearchRequestContext.parse({**node.ledger.identity.model_dump(), "actor_id": "actor-1",
                                       "client_id": "client-2", "grant_id": envelope.grant_id,
                                       "assignment_id": envelope.assignment_id, "request_id": envelope.request_id,
                                       "request_type": "permissions.v2.search"})
