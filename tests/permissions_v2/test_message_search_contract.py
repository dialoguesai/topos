"""p2c-v1's closed grammar: a grant searches only when its signed policy says so, and only at raw.

Also pins the separation from the existing doors: a p2a or p2b document never
parses as a search policy and never gains search by adding a key; a p2c envelope
never reaches the locator or fact adapters; the p2a checkpoint refuses a search.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.contract import PolicyV2
from topos.permissions_v2.registry import AttestedSubjectSourcePolicy, parse_decision, parse_disclosure, parse_policy
from topos.permissions_v2.release import SOURCE_DECISIONS, parse_source_envelope
from topos.permissions_v2.search_contract import (MessageSearchResult, SearchIntent, SearchPolicy, SearchSetDecision,
    search_capability_document, signed_payload)
from topos.permissions_v2.signing import SearchAuthorityBinding, parse_authority, parse_envelope


def test_search_policy_parses_through_the_closed_registry_only_by_its_literal():
    raw = mc.search_policy()
    assert isinstance(parse_policy(raw), SearchPolicy)
    wrong = deepcopy(raw)
    wrong["versions"]["capability"] = "permissions-beta/p2a-v2"
    with pytest.raises(PolicyError):
        parse_policy(wrong)
    assert "permissions-beta/p2c-v1" in SOURCE_DECISIONS


@pytest.mark.parametrize("mutate", [
    lambda p: p["rules"][0]["release"].update(ceiling="summary"),
    lambda p: p["rules"][0]["release"].update(ceiling="inference"),
    lambda p: p["search"].update(max_permitted_records=5_001),
    lambda p: p["search"].update(max_permitted_records=0),
    lambda p: p["search"].update(max_k=26),
    lambda p: p["search"].update(tables=[]),
    lambda p: p["search"].update(tables=["ai_chat_messages"]),                 # not covered by a permit rule
    lambda p: p["search"].update(view_id="canonical.message_disclosure.v1"),
    lambda p: p["search"]["window"].update(missing_or_ambiguous="include"),
    lambda p: p["search"].update(extra=1),
    lambda p: p.pop("search"),
    lambda p: p["rules"][0]["release"]["forms"][0].update(operation="read"),
    lambda p: p["rules"][0]["release"]["forms"][0].update(view_id="canonical.message_disclosure.v1"),
    lambda p: p["rules"][0]["evidence_use"]["sources"].update(values=["not-in-universe"]),
    lambda p: p["evaluator"].update(version="hard-rules/p2a-v2"),
    lambda p: p["versions"].update(vocabulary="content-vocabulary/v1"),
    lambda p: p.update(natural_language="search anything"),
])
def test_malformed_search_policies_refuse(mutate):
    raw = mc.search_policy()
    mutate(raw)
    with pytest.raises(PolicyError, match="schema_invalid"):
        parse_policy(raw)


def test_no_existing_grant_gains_search_by_adding_a_key():
    p2a = mc.p2a_v2_policy()
    p2a["search"] = mc.search_policy()["search"]
    with pytest.raises(PolicyError, match="schema_invalid"):
        parse_policy(p2a)
    with pytest.raises(PolicyError):
        AttestedSubjectSourcePolicy.parse(mc.search_policy())
    with pytest.raises(PolicyError):
        PolicyV2.parse(mc.search_policy())


@pytest.mark.parametrize("payload", [
    {"query": "x", "k": 0}, {"query": "x", "k": 26}, {"query": "x", "k": "5"}, {"query": "x", "k": 5.0},
    {"query": "x"}, {"query": "", "k": 1}, {"query": "x" * 8_001, "k": 1}, {"query": 5, "k": 1},
    {"query": "x", "k": 1, "source_id": "imessage"}, {"query": "x", "k": 1, "grant_id": "g"},
    {"query": "x", "k": 1, "view_id": "canonical.message_search.v1"}, {"query": "x", "k": 1, "ceiling": "raw"},
    {"query": "x", "k": 1, "window": {"after": 10, "before": 10}}, {"query": "x", "k": 1, "window": {"after": -1, "before": 2}},
    {"query": "x", "k": 1, "window": {"after": 1}}, {"query": "x", "k": 1, "window": {"after": 1, "before": 2, "tz": "utc"}},
    {"query": "x", "k": True},
])
def test_intent_schema_is_closed(payload):
    # schema_invalid, or json_type where canonical JSON already refuses (a float)
    with pytest.raises(PolicyError, match="schema_invalid|json_type"):
        SearchIntent.parse(payload)


def test_signed_payload_is_one_canonical_form():
    assert signed_payload(SearchIntent.parse({"query": "x", "k": 3})) == {"query": "x", "k": 3}
    assert signed_payload(SearchIntent.parse({"query": "x", "k": 3, "window": {"after": 1, "before": 2}})) == \
        {"query": "x", "k": 3, "window": {"after": 1, "before": 2}}


def test_view_has_exactly_five_record_fields_and_no_counts_scores_or_reasons():
    schema = MessageSearchResult.model_json_schema()
    record = schema["$defs"]["SearchRecord"]["properties"]
    assert set(record) == {"record_id", "source_id", "canonical_table", "event_at", "content"}
    assert set(schema["properties"]) == {"family", "operation", "view_id", "records"}
    integer_fields = [name for name, spec in record.items() if spec.get("type") == "integer"]
    assert integer_fields == ["event_at"]
    ok = {"family": "canonical_record", "operation": "search", "view_id": "canonical.message_search.v1", "records": []}
    MessageSearchResult.parse(ok)
    for extra in ({"total": 3}, {"truncated": False}, {"scores": []}, {"deny_reason": "x"}, {"ledger": []}):
        with pytest.raises(PolicyError):
            MessageSearchResult.parse({**ok, **extra})
    good = {"record_id": "r." + "a" * 64, "source_id": "imessage", "canonical_table": "conversation_messages",
            "event_at": 1, "content": "x"}
    MessageSearchResult.parse({**ok, "records": [good]})
    for bad in ({"record_id": "imessage:123"}, {"score": 0.5}, {"record_id": "r." + "A" * 64}, {"event_at": -1}):
        with pytest.raises(PolicyError):
            MessageSearchResult.parse({**ok, "records": [{**good, **bad}]})
    with pytest.raises(PolicyError):
        MessageSearchResult.parse({**ok, "records": [good] * 26})


def test_set_decision_is_coherent():
    base = {"stage": "output_release", "verdict": "permit", "policy_hash": "a" * 64, "candidate_revision": "b" * 64,
            "evaluator_version": "hard-rules/p2c-v1", "matched_allow_clause_ids": ["r1"], "matched_deny_clause_ids": [],
            "reason_code": "rule_permit", "required_projection_id": "canonical.message_search.v1", "member_count": 1,
            "missing_context_codes": []}
    SearchSetDecision.parse(base)
    for bad in ({"reason_code": "set_refused"}, {"required_projection_id": None}, {"matched_deny_clause_ids": ["x"]},
                {"matched_allow_clause_ids": ["b", "a"]}, {"missing_context_codes": ["classification"]},
                {"verdict": "deny", "reason_code": "set_refused", "required_projection_id": None}):
        with pytest.raises(PolicyError):
            SearchSetDecision.parse({**base, **bad})
    assert parse_decision(base, capability="permissions-beta/p2c-v1").member_count == 1


def test_search_authority_and_envelope_are_their_own_classes():
    raw = {**mc.search_policy()["binding"], "grant_generation": 1, "assignment_generation": 1, "policy_version_id": "p",
           "policy_hash": "a" * 64, "capability_version": "permissions-beta/p2c-v1", "protection_revision": "b" * 64,
           "node_epoch": 1}
    assert isinstance(parse_authority(raw), SearchAuthorityBinding)
    envelope = {**raw, "version": "topos-grantee-envelope/v2", "kid": "k", "request_id": "r",
                "request_type": "permissions.v2.search", "request_hash": "c" * 64, "issued_at": 1, "expires_at": 2}
    parse_envelope(envelope, signed=False)
    for request_type in ("permissions.v2.read", "permissions.v2.fact.read", "permissions.v2.preview"):
        with pytest.raises(PolicyError):
            parse_envelope({**envelope, "request_type": request_type}, signed=False)
    # A p2c envelope is never a locator-door envelope.
    with pytest.raises(PolicyError):
        parse_source_envelope({**envelope, "signature": "A" * 86})


def test_disclosure_dispatch_and_capability_document():
    empty = {"family": "canonical_record", "operation": "search", "view_id": "canonical.message_search.v1", "records": []}
    assert isinstance(parse_disclosure(empty, capability="permissions-beta/p2c-v1"), MessageSearchResult)
    with pytest.raises(PolicyError):
        parse_disclosure(empty, capability="permissions-beta/p2a-v2")
    document = search_capability_document()
    assert document["capabilities"] == ["permissions-beta/p2c-v1"] and document["ceilings"] == ["raw"]


@pytest.mark.parametrize("model", __import__("tests.permissions_v2.message_search_schemas",
                                             fromlist=["SEARCH_MODELS"]).SEARCH_MODELS, ids=lambda m: m.__name__)
def test_search_schema_exports_are_pinned(model):
    from tests.permissions_v2.message_search_schemas import SEARCH_FIXTURES
    from tests.permissions_v2.source_attested_schemas import export
    assert (SEARCH_FIXTURES / f"{model.__name__}.schema.json").read_bytes() == export(model)
