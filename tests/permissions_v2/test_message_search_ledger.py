"""checkpoint_set_decision: one decision and one v3 receipt per search, and the shape it refuses."""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner, recipient
from tests.permissions_v2.test_message_search_refusals import PAYLOAD, signed
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.search_contract import SearchIntent, signed_payload
from topos.permissions_v2.signing import SearchRequestContext


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=23, counts={"clean_positive_C": 4, "other_domain": 2})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


def test_a_search_writes_one_v3_receipt_binding_the_set(node):
    output, refused = node.search_request("roadmap deploy review", k=3, request_id="s-1")
    assert refused is None
    with sqlite3.connect(node.ledger.path) as conn:
        receipt, decision = conn.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id='s-1'").fetchone()
        status = conn.execute("SELECT status FROM p2a_requests WHERE request_id='s-1'").fetchone()[0]
    receipt, decision = json.loads(receipt), json.loads(decision)
    assert status == "checkpointed"
    assert receipt["version"] == "topos-local-receipt/v3" and receipt["verdict"] == "permit"
    assert receipt["record_count"] == len(output["records"]) == decision["member_count"]
    assert receipt["output_hash"] == digest(output)
    assert decision["matched_allow_clause_ids"] == ["permit-work-not-personal"]
    text = json.dumps(receipt) + json.dumps(decision)
    for record in output["records"]:
        assert record["record_id"] not in text and record["content"] not in text
    assert "roadmap" not in text


def test_an_empty_answer_is_a_normal_permit_with_a_receipt(node):
    output, refused = node.search_request("zzzz nothing matches", k=3, request_id="s-empty")
    assert refused is None and output["records"] == []
    with sqlite3.connect(node.ledger.path) as conn:
        receipt = json.loads(conn.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id='s-empty'").fetchone()[0])
    assert receipt["verdict"] == "permit" and receipt["record_count"] == 0


def admitted(node, request_id="s-direct"):
    envelope = signed(node, request_id=request_id)
    request = SearchRequestContext.parse({**node.ledger.identity.model_dump(), "actor_id": "actor-1", "client_id": "client-2",
        "grant_id": "grant-search", "assignment_id": "assignment-grant-search", "request_id": request_id,
        "request_type": "permissions.v2.search"})
    lease = node.ledger.admit(envelope.model_dump(), request=request, payload=signed_payload(SearchIntent.parse(PAYLOAD)),
                              now=node.now[0])
    with node.ledger._transaction() as db:
        authority, _ = node.ledger._authority(db, "grant-search", node.now[0])
    return lease, authority


def decision(authority, members, **changes):
    value = {"stage": "output_release", "verdict": "permit", "policy_hash": authority.policy_hash,
             "candidate_revision": "c" * 64, "evaluator_version": "hard-rules/p2c-v1",
             "matched_allow_clause_ids": sorted({m["allow_clause_id"] for m in members}), "matched_deny_clause_ids": [],
             "reason_code": "rule_permit", "required_projection_id": "canonical.message_search.v1",
             "member_count": len(members), "missing_context_codes": []}
    value.update(changes)
    return value


def record(**changes):
    return {"record_id": "r." + "a" * 64, "source_id": "imessage", "canonical_table": "conversation_messages",
            "event_at": 1, "content": "x", **changes}


def member(**changes):
    return {"table": "conversation_messages", "source_id": "imessage", "record_id": "imessage:1", "fact_id": "f",
            "allow_clause_id": "permit-work-not-personal", "member_decision_hash": "d" * 64, **changes}


def output(*records):
    return {"family": "canonical_record", "operation": "search", "view_id": "canonical.message_search.v1",
            "records": list(records)}


@pytest.mark.parametrize("records, members, error", [
    ([record()], [], "decision_inconsistent"),                                        # a record with no member
    ([record()], [member(), member()], "decision_inconsistent"),                      # misaligned
    ([record()], [member(allow_clause_id="deny-private-domains")], "rule_binding"),    # a deny rule as the permit
    ([record()], [member(allow_clause_id="no-such-rule")], "rule_binding"),
    ([record(source_id="signal")], [member(source_id="signal")], "rule_binding"),       # source outside the rule
    ([record(canonical_table="ai_chat_messages")], [member(table="ai_chat_messages")], "rule_binding"),
    ([record(source_id="signal")], [member()], "rule_binding"),                          # member and record disagree
])
def test_the_set_shape_is_checked_per_record(node, records, members, error):
    lease, authority = admitted(node)
    with pytest.raises(PolicyError, match=error):
        node.ledger.checkpoint_set_decision(lease, decision(authority, members),
            candidate_revision="c" * 64, output=output(*records), members=members, now=node.now[0])


def test_decision_binding_and_replay(node):
    lease, authority = admitted(node)
    with pytest.raises(PolicyError, match="decision_binding"):
        node.ledger.checkpoint_set_decision(lease, decision(authority, []), candidate_revision="e" * 64,
                                            output=output(), members=[], now=node.now[0])
    with pytest.raises(PolicyError, match="decision_binding"):
        node.ledger.checkpoint_set_decision(lease, decision(authority, [], policy_hash="f" * 64), candidate_revision="c" * 64,
                                            output=output(), members=[], now=node.now[0])
    node.ledger.checkpoint_set_decision(lease, decision(authority, []), candidate_revision="c" * 64, output=output(),
                                        members=[], now=node.now[0])
    with pytest.raises(PolicyError, match="request_replay"):
        node.ledger.checkpoint_set_decision(lease, decision(authority, []), candidate_revision="c" * 64, output=output(),
                                            members=[], now=node.now[0])


def test_a_denied_set_carries_no_output_or_members(node):
    lease, authority = admitted(node)
    deny = decision(authority, [], verdict="deny", reason_code="set_refused", required_projection_id=None)
    with pytest.raises(PolicyError, match="denied_output"):
        node.ledger.checkpoint_set_decision(lease, deny, candidate_revision="c" * 64, output=output(), members=[],
                                            now=node.now[0])


def test_the_single_read_checkpoint_refuses_a_search_lease(node):
    lease, authority = admitted(node)
    with pytest.raises(PolicyError, match="unsupported_capability"):
        node.ledger.checkpoint_decision(lease, decision(authority, []), candidate_revision="c" * 64, output=output(),
                                        now=node.now[0])
