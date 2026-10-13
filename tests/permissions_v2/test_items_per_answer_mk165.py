"""MK165 (13 Oct 2026): the owner's "Items per answer" is the grant's `max_k`.

The share form offers 5, 10, 20, 30 or 50 (30 for a new share); the control plane signs the choice as the
knowledge declaration's `max_k`, and this node enforces it as it always did: per search (`search_release._bounds`),
and for an answer as `min(max_k, ANSWER_ITEMS)` (`retrieve_for_answer`). What moves here is the ceiling:
`KNOWLEDGE_MAX_K` 20 -> 50, and the request grammar's `MAX_K_CEILING` 25 -> 50 with it, so a recipient may ask the
most a grant can sign. What does not move: a grant keeps the `max_k` it signed; a k above it is the uniform refusal;
a policy above this node's ceiling is refused at parse, so an older node (pinned at 20) never applies a policy
compiled at 30 and the share waits on activation instead of widening. The heartbeat says the ceiling, so the
control plane compiles no share above it.
"""
from __future__ import annotations

import base64
import copy

import pytest

from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest
from topos.permissions_v2.knowledge_contract import KNOWLEDGE_MAX_K, KnowledgeSearchResult, KnowledgeSetDecision
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_contract import MAX_K_CEILING, SearchIntent
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_contract import distinct, set_decision
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from tests.permissions_v2.test_node_protocol import protocol  # noqa: F401 (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)

P = "TOPOS_PERMISSIONS_V2_"


# --- the ceiling ----------------------------------------------------------------------------------------------------

def test_the_ceiling_is_fifty_and_the_request_grammar_reaches_it():
    """The top of the owner's choices, and a k the grammar admits; one more is a schema refusal, not a grant's."""
    assert KNOWLEDGE_MAX_K == 50
    assert MAX_K_CEILING == 50, "a recipient must be able to ask the most a grant can sign"
    assert SearchIntent.parse({"query": "x", "k": MAX_K_CEILING}).k == 50
    with pytest.raises(PolicyError, match="schema_invalid"):
        SearchIntent.parse({"query": "x", "k": MAX_K_CEILING + 1})


@pytest.mark.parametrize("max_k", [5, 10, 20, 30, 50])
def test_a_knowledge_grant_may_sign_each_items_per_answer_choice(max_k):
    assert parse_policy(knowledge_policy(max_k)).search.max_k == max_k


@pytest.mark.parametrize("max_k", [0, KNOWLEDGE_MAX_K + 1, 100])
def test_a_knowledge_grant_outside_one_to_fifty_is_refused_at_parse(max_k):
    """What a node pinned lower does with a policy above its own ceiling: the same bound, a smaller number."""
    with pytest.raises(PolicyError, match="schema_invalid"):
        parse_policy(knowledge_policy(max_k))


def test_the_three_knowledge_bounds_move_together():
    """The declaration's max_k, the result's records and the set decision's member_count: a grant signed at 50 answers
    50 records in one page, and the decision names them. Raising only the declaration failed every k above the old
    bound at output parse (C2's lesson)."""
    assert len(KnowledgeSearchResult.parse(distinct(KNOWLEDGE_MAX_K)).records) == 50
    with pytest.raises(PolicyError):
        KnowledgeSearchResult.parse(distinct(KNOWLEDGE_MAX_K + 1))
    assert KnowledgeSetDecision.parse(set_decision(KNOWLEDGE_MAX_K)).member_count == 50
    with pytest.raises(PolicyError):
        KnowledgeSetDecision.parse(set_decision(KNOWLEDGE_MAX_K + 1))


# --- the heartbeat --------------------------------------------------------------------------------------------------

def test_the_heartbeat_advertises_the_ceiling(monkeypatch):
    """`permissions_v2_knowledge_max_k`: the control plane compiles no share above what the node says, so a node
    behind this release (which says nothing) is offered its old 20 and never a policy it would refuse."""
    from topos.engine import registration
    assert registration._knowledge_max_k() == KNOWLEDGE_MAX_K == 50
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    assert registration.build_engine_capabilities()["permissions_v2_knowledge_max_k"] == 50


# --- a policy above the node's ceiling never applies -----------------------------------------------------------------

def test_a_mutation_carrying_a_policy_above_the_ceiling_is_refused_at_parse_and_applies_nothing(protocol):
    """The old-node case, run against this node's own bound: the control plane signs a mutation whose policy's max_k
    is one above what this node admits. `mutate` refuses it as `schema_invalid` before the ledger is touched (the
    handler answers the relay `permissions_v2_mutate` an error, never an ack), the grant stays absent, and the same
    command at the ceiling then applies. A node pinned at 20 treats a policy at 30 exactly so."""
    from tests.permissions_v2.test_node_protocol import mutation, status_request
    node, fixture_policy, cp_key, _ = protocol
    raw = knowledge_policy(KNOWLEDGE_MAX_K)
    raw["binding"] = copy.deepcopy(fixture_policy["binding"])
    raw = parse_policy(raw).model_dump()  # the signed form: the hash in the authority is of these bytes
    now = raw["validity"]["starts_at"] + 60  # inside the policy's validity, so only the parse can refuse it
    signed = mutation(protocol, policy=raw, now=now)
    # The same command bytes, with max_k one above the bound, re-hashed and re-signed: what a newer control plane
    # would send a node that has not moved. (The grammar refuses to build it, so it is built by hand.)
    body = signed.model_dump(exclude={"signature"})
    body["policy"]["search"]["max_k"] = KNOWLEDGE_MAX_K + 1
    body["authority"]["policy_hash"] = digest(body["policy"])
    signing = (body["version"] + "\n").encode("ascii") + canonical_bytes(body)
    body["signature"] = base64.urlsafe_b64encode(cp_key.sign(signing)).decode("ascii").rstrip("=")
    with pytest.raises(PolicyError, match="schema_invalid"):
        node.mutate(body, now=now)
    state = node.status(status_request(protocol, now=now).model_dump(), now=now)
    assert state.state.grant_state == "absent" and state.state.node_epoch == 0, "nothing applied, nothing moved"
    # At the ceiling the very same command applies.
    ack = node.mutate(signed.model_dump(), now=now)
    assert ack.outcome == "applied" and ack.state.grant_state == "active"
    assert ack.receipt.authority.policy_hash == digest(raw)


# --- the answer side: min(max_k, ANSWER_ITEMS) ----------------------------------------------------------------------

@pytest.mark.parametrize("max_k, settings", [
    (30, (("8", 8), ("16", 16), ("24", 24))),   # the new default: the switch binds, the grant does not
    (5, (("8", 5), ("16", 5), ("3", 3))),        # the smallest choice: the grant binds
    (50, (("8", 8), ("24", 24), ("99", 24))),    # the ceiling: the switch's own ceiling (24) binds
])
def test_an_answer_retrieves_the_smaller_of_the_grants_max_k_and_answer_items(legacy, tmp_path, monkeypatch, max_k, settings):
    """Unchanged in meaning (A2A-4 §2 `K_ANSWER`, amendment 11.7): the owner's number is a ceiling on an answer's
    items, never a floor; `ANSWER_ITEMS` (another lane's) is read as before."""
    from tests.permissions_v2.message_search_harness import owner, recipient
    from tests.permissions_v2.test_knowledge_search import node_for
    from topos.permissions_v2 import search_release
    seen = []
    original = search_release.SearchIntent.parse

    def parse(value):
        seen.append(value.get("k"))
        return original(value)

    monkeypatch.setattr(search_release.SearchIntent, "parse", parse)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=max_k, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    with node.ledger._transaction() as db:
        authority, policy = node.ledger._authority(db, "grant-search", node.now[0])
    assert policy.search.max_k == max_k
    for setting, expected in settings:
        monkeypatch.setenv(P + "ANSWER_ITEMS", setting)
        seen.clear()
        with recipient():
            node.search.retrieve_for_answer(grant_id="grant-search", question="Synthetic message", admitted_authority=authority)
        assert seen[0] == expected, (max_k, setting, seen)


# --- the search side: a grant signed at the new default and at the ceiling ---------------------------------------------

@pytest.mark.parametrize("max_k", [30, 50])
def test_a_grant_signed_at_thirty_or_fifty_answers_k_up_to_it_and_refuses_one_more(legacy, tmp_path, monkeypatch, max_k):
    """Per search, as before (C2's test at 20): k up to the grant's own max_k is answered; one more is the uniform
    refusal with the one deny receipt. At 50 one more is outside the grammar and refused as a schema error."""
    from tests.permissions_v2.test_knowledge_search import deny_receipt, node_for
    node, _ = node_for(legacy, tmp_path, monkeypatch, max_k=max_k)
    node.rebuild()
    output, refused = node.search_request("Synthetic message", k=max_k)
    assert refused is None and len(output["records"]) == 1
    output, refused = node.search_request("Synthetic message", k=max_k + 1, request_id="k-over")
    assert output is None and refused is not None
    if max_k < MAX_K_CEILING:
        assert refused == "permission_denied"
        assert deny_receipt(node, "k-over") == ("deny", None, 0, "set_refused")
