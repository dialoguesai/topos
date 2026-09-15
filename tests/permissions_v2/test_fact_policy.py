"""P2b policy/time boundaries with real scratch resolver-owned evidence.

These test pure decisions; no capability, ledger or recipient path is enabled.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

import pytest

from tests.permissions_v2.test_evidence import corpus, attest, edit
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import Binding, PolicyV2, capability_document
from topos.permissions_v2.evidence import Qualification, _key
from topos.permissions_v2.fact_policy import (
    FactPolicyV2, FactDecision, canonical_utc_microseconds, fact_projection_decision,
)
from topos.permissions_v2.fact_projection import (
    FactProjectionReview, bind_output_review, prepare_fact_projection,
)
from topos.permissions_v2.fact_contract import FactScalarDisclosure
from topos.features.facts.store import FactStore

AS_OF = 1_800_000_000


def utc(seconds, micros=0):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds, microseconds=micros)).isoformat().replace("+00:00", "Z")


def atom(domain):
    return {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": [domain]}


def rule(name="allow-reading", effect="permit", domain="reading"):
    return {"rule_id": name, "effect": effect, "evidence_use": {
        "sources": {"kind": "only", "values": ["source-1", "ai-source-1"]},
        "tables": ["signal_objects", "conversation_messages", "ai_chat_messages"],
        "event_window": {"kind": "rolling", "anchor": "server_request_as_of", "max_age_seconds": 86400,
            "event_time_semantics": "canonical_event_time_v1", "missing_or_ambiguous": "withhold", "future": "withhold"},
        "predicate": atom(domain), "purpose": "owner-stated-fact-projection",
        "processors": {"kind": "only", "values": ["owner-engine-local"]}, "new_records": "include_if_predicate"},
        "release": {"predicate": atom(domain), "ceiling": "summary", "forms": [
            {"family": "owner_stated_fact", "operation": "read", "view_id": "owner_stated_fact.scalar.v1"}]}}


def policy(corpus):
    return {"version": "topos-policy/v2", "policy_version_id": "fact-policy-1",
        "binding": {**corpus[0].binding.model_dump(), "actor_id": "actor-1", "client_id": "client-1",
            "grant_id": "grant-1", "assignment_id": "assignment-1"},
        "versions": {"vocabulary": "owner-review-vocabulary/v1", "capability": "permissions-beta/p2b-v1"},
        "validity": {"starts_at": AS_OF-3600, "expires_at": AS_OF+3600},
        "source_universe": {"universe_id": "universe-1", "revision": 1, "source_ids": ["source-1", "ai-source-1"]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold", "unknown_lineage": "withhold",
            "cross_rule_derivation": "deny", "capability_growth": "require_consent"},
        "rules": [rule()], "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2b-v1"}, "natural_language": None}


@pytest.mark.parametrize("model", [FactPolicyV2,FactDecision,FactScalarDisclosure])
def test_portable_closed_schema_exports_are_frozen(model):
    path=Path(__file__).resolve().parents[2]/"fixtures"/"permissions_v2"/"fact_policy"/(model.__name__+".schema.json")
    assert json.loads(path.read_text())==model.model_json_schema()


@pytest.fixture
def timed(corpus):
    for table in ("conversation_messages", "ai_chat_messages"):
        edit(corpus, f"ALTER TABLE {table} ADD COLUMN event_at TEXT")
        edit(corpus, f"UPDATE {table} SET event_at=?", (utc(AS_OF-100),))
    edit(corpus, "UPDATE signal_objects SET valid_from=?", (utc(AS_OF-50),))
    return corpus


def bundle(corpus, *, transform=None, output_domain="reading"):
    attest(corpus, transform=transform)
    evidence, rows = corpus[0].with_qualified(corpus[2], reviews=corpus[1], callback=lambda evidence, rows: (evidence, rows))
    qualification = Qualification(verdict="qualified", reason_code="test-current-resolver", evidence=evidence)
    root = next(ref for ref in evidence.snapshot.artifacts if ref.identity.record_id == corpus[2])
    fact = rows[_key(root.identity)]
    candidate = prepare_fact_projection(qualification=qualification, fact_row=fact)
    review = FactProjectionReview.parse({"version": "topos-fact-projection-review/v1", "review_id": "output-1",
        "owner_id": corpus[0].binding.owner_id, "reviewed_at": AS_OF-1, "status": "approved",
        "candidate": candidate.model_dump(), "candidate_hash": digest(candidate.model_dump()),
        "output_hash": digest(candidate.output.model_dump()), "classification": {"domains": [output_domain],
            "sensitivity": "personal", "subject": "self", "assertion": "explicit_atomic_preference"}})
    projection = bind_output_review(qualification=qualification, fact_row=fact, review=review, now=AS_OF)
    return dict(evidence=evidence, projection=projection, rows=rows)


def evaluate(corpus, raw=None, **options):
    raw = raw or policy(corpus)
    supplied = options.pop("supplied", None) or bundle(corpus)
    return fact_projection_decision(policy=FactPolicyV2.parse(raw), **supplied,
        binding=options.pop("binding", Binding.parse(raw["binding"])),
        request_as_of=options.pop("request_as_of", AS_OF), now=options.pop("now", AS_OF), **options)


@pytest.mark.parametrize("ceiling", ["summary", "raw"])
def test_exact_fact_view_works_without_raw_source_output(timed, ceiling):
    raw = policy(timed)
    raw["rules"][0]["release"]["ceiling"] = ceiling
    result = evaluate(timed, raw)
    assert result.verdict == "permit"
    assert result.required_projection_id == "owner_stated_fact.scalar.v1"
    assert result.matched_allow_clause_ids == ["allow-reading"]
    assert "history books" not in result.model_dump_json()
    assert capability_document()["version"] == "permissions-beta/p2a-v1"
    assert capability_document()["executable_forms"] == []
    with pytest.raises(PolicyError):
        PolicyV2.parse(raw)


def test_inference_ceiling_never_grants_the_fact_view(timed):
    raw = policy(timed)
    raw["rules"][0]["release"]["ceiling"] = "inference"
    result = evaluate(timed, raw)
    assert result.verdict == "deny" and result.reason_code == "unsupported_view"


@pytest.mark.parametrize("change", ["missing_tables", "missing_window", "output_tables", "legacy_capability", "legacy_evaluator",
    "nl", "unknown_field", "purpose", "processor", "universe_revision", "source_outside", "duplicate_table", "duplicate_rule"])
def test_new_policy_is_closed_and_requires_explicit_opt_in(timed, change):
    raw = policy(timed)
    evidence = raw["rules"][0]["evidence_use"]
    if change == "missing_tables": evidence.pop("tables")
    elif change == "missing_window": evidence.pop("event_window")
    elif change == "output_tables": raw["rules"][0]["release"]["forms"][0]["tables"] = ["conversation_messages"]
    elif change == "legacy_capability": raw["versions"]["capability"] = "permissions-beta/p2a-v1"
    elif change == "legacy_evaluator": raw["evaluator"]["version"] = "hard-rules/p2a-v1"
    elif change == "nl": raw["natural_language"] = {"text": "allow everything"}
    elif change == "unknown_field": raw["ignore_protection"] = True
    elif change == "purpose": evidence["purpose"] = "arbitrary-purpose"
    elif change == "processor": evidence["processors"]["values"] = ["remote-model"]
    elif change == "universe_revision": evidence["sources"] = {"kind":"all", "universe_id":"universe-1", "universe_revision":2, "growth":"require_consent"}
    elif change == "source_outside": evidence["sources"]["values"] = ["unknown-source"]
    elif change == "duplicate_table": evidence["tables"] = ["signal_objects", "signal_objects"]
    else: raw["rules"].append(deepcopy(raw["rules"][0]))
    with pytest.raises(PolicyError): FactPolicyV2.parse(raw)


@pytest.mark.parametrize("field,value", [("max_age_seconds",0),("max_age_seconds",True),("max_age_seconds",1.5),
    ("max_age_seconds",9007199254740992),("anchor","recipient_as_of"),("kind","unrestricted"),
    ("missing_or_ambiguous","allow"),("future","allow"),("event_time_semantics","created_at_fallback")])
def test_time_policy_requires_exact_bounded_semantics(timed, field, value):
    raw=policy(timed)
    raw["rules"][0]["evidence_use"]["event_window"][field]=value
    with pytest.raises(PolicyError): FactPolicyV2.parse(raw)


@pytest.mark.parametrize("empty", ["sources", "tables", "forms", "processors", "rules"])
def test_explicit_empty_selection_never_expands(timed, empty):
    raw=policy(timed)
    if empty == "rules": raw["rules"]=[]
    elif empty == "forms": raw["rules"][0]["release"]["forms"]=[]
    elif empty in {"sources", "processors"}: raw["rules"][0]["evidence_use"][empty]["values"]=[]
    else: raw["rules"][0]["evidence_use"][empty]=[]
    assert evaluate(timed,raw).verdict == "deny"


@pytest.mark.parametrize("bad", [None,"",0,True,"1800000000","2027-01-15", "2027-01-15T08:00:00",
    "2027-01-15 08:00:00Z", "2027-01-15T08:00:00-00:00", "2027-01-15T09:00:00+01:00",
    "2027-02-30T08:00:00Z", "2027-01-15T08:00:60Z", "2027-01-15T08:00:00.1234567Z", "2027-01-15T08:00:00Z\n"])
def test_canonical_time_rejects_ambiguous_or_non_utc_values(timed, bad):
    assert canonical_utc_microseconds(bad) is None
    edit(timed,"UPDATE conversation_messages SET event_at=?", (bad,))
    result=evaluate(timed)
    assert result.verdict == "indeterminate" and "time" in result.missing_context_codes


@pytest.mark.parametrize("delta,micro,expected", [(-86400,0,"permit"),(-86400,1,"permit"),(-86400,-1,"deny"),
    (-1,999999,"permit"),(0,0,"permit"),(0,1,"indeterminate"),(1,0,"indeterminate")])
def test_inclusive_window_and_future_boundaries_are_exact(timed,delta,micro,expected):
    edit(timed,"UPDATE conversation_messages SET event_at=?", (utc(AS_OF+delta,micro),))
    assert evaluate(timed,now=AS_OF+120).verdict == expected


def test_utc_offset_zero_and_fraction_preserve_exact_time(timed):
    text=utc(AS_OF-86400,1).replace("Z","+00:00")
    assert canonical_utc_microseconds(text)==(AS_OF-86400)*1_000_000+1
    edit(timed,"UPDATE conversation_messages SET event_at=?", (text,))
    assert evaluate(timed).verdict=="permit"


def test_missing_event_time_has_no_creation_or_epoch_fallback(timed):
    edit(timed,"ALTER TABLE conversation_messages ADD COLUMN created_at TEXT")
    edit(timed,"UPDATE conversation_messages SET event_at=NULL,created_at=?", (utc(AS_OF),))
    assert evaluate(timed).verdict=="indeterminate"


@pytest.mark.parametrize("start,expected", [(None,"indeterminate"),("unparseable","indeterminate"),
    (utc(AS_OF,1),"deny"),(utc(AS_OF),"permit")])
def test_fact_valid_from_is_independent_of_leaf_event_window(timed,start,expected):
    if start is None:
        # Native NOT NULL schema cannot manufacture a missing timestamp; empty
        # text exercises its semantically unknown equivalent.
        start=""
    edit(timed,"UPDATE signal_objects SET valid_from=?", (start,))
    assert evaluate(timed).verdict==expected


def test_closed_fact_never_reuses_previously_qualified_projection(timed):
    supplied=bundle(timed)
    edit(timed,"UPDATE signal_objects SET valid_to=?", (utc(AS_OF+100),))
    assert timed[0].qualify(timed[2],reviews=timed[1]).verdict=="withheld"
    root=next(ref for ref in supplied["evidence"].snapshot.artifacts if ref.identity.record_id==timed[2])
    supplied["rows"][_key(root.identity)]["valid_to"]=utc(AS_OF+100)
    with pytest.raises(PolicyError,match="fact_policy_revision"):
        evaluate(timed,supplied=supplied)


@pytest.mark.parametrize("axis", ["environment_id","node_id","resource_id","owner_id","actor_id","client_id","grant_id","assignment_id"])
def test_every_authority_binding_axis_must_match(timed,axis):
    raw=policy(timed)
    actual=Binding.parse({**raw["binding"],axis:"another"})
    with pytest.raises(PolicyError,match="fact_policy_binding"):
        evaluate(timed,raw,binding=actual)


@pytest.mark.parametrize("now", [AS_OF-1,AS_OF+121])
def test_future_or_stale_request_anchor_withholds(timed,now):
    assert evaluate(timed,now=now).reason_code=="stale_authority"


@pytest.mark.parametrize("value", [True,1.5,-1,9007199254740992])
def test_request_anchor_is_a_strict_safe_integer(timed,value):
    with pytest.raises(PolicyError): evaluate(timed,request_as_of=value)


def test_policy_expiry_during_execution_withholds(timed):
    raw=policy(timed)
    raw["validity"]["expires_at"]=AS_OF+10
    assert evaluate(timed,raw,now=AS_OF+10).reason_code=="stale_authority"


def two_leaves(timed):
    edit(timed,"UPDATE signal_objects SET source_refs_json=?", (json.dumps([
        {"table":"conversation_messages","record_id":"message-1","source_id":"source-1","dataset_id":"dataset-1"},
        {"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}]),))


@pytest.mark.parametrize("axis", ["sources","tables"])
def test_two_partial_permits_cannot_be_stitched(timed,axis):
    two_leaves(timed)
    raw=policy(timed)
    raw["rules"]=[rule("first"),rule("second")]
    if axis=="sources":
        raw["rules"][0]["evidence_use"]["sources"]["values"]=["source-1"]
        raw["rules"][1]["evidence_use"]["sources"]["values"]=["ai-source-1"]
    else:
        raw["rules"][0]["evidence_use"]["tables"]=["signal_objects","conversation_messages"]
        raw["rules"][1]["evidence_use"]["tables"]=["signal_objects","ai_chat_messages"]
    assert evaluate(timed,raw).verdict=="deny"


def test_artifact_table_is_required_separately_from_leaf_tables(timed):
    raw=policy(timed)
    raw["rules"][0]["evidence_use"]["tables"]=["conversation_messages"]
    assert evaluate(timed,raw).verdict=="deny"


def test_all_source_selection_uses_only_the_pinned_universe(timed):
    raw=policy(timed)
    raw["rules"][0]["evidence_use"]["sources"]={"kind":"all", "universe_id":"universe-1",
        "universe_revision":1,"growth":"require_consent"}
    assert evaluate(timed,raw).verdict=="permit"
    raw["source_universe"]["source_ids"]=[]
    assert evaluate(timed,raw).verdict=="deny"


@pytest.mark.parametrize("source,window,expected", [("source-1",86400,"permit"),
    ("ai-source-1",86400,"permit"),("ai-source-1",172800,"deny")])
def test_derived_deny_uses_its_own_descendant_source_and_time(timed,source,window,expected):
    with sqlite3.connect(timed[0].path) as conn:
        child=FactStore(conn).assert_fact(subject_entity_id="self",predicate="member_of",object_value="reading group",
            disclosure="scoped",asserted_by="owner",valid_from=utc(AS_OF-100),
            source_refs=[{"table":"ai_chat_messages","record_id":"ai-message-1","source_id":"ai-source-1"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?",(json.dumps([
            {"table":"signal_objects","record_id":child["object_id"]},
            {"table":"conversation_messages","record_id":"message-1","source_id":"source-1","dataset_id":"dataset-1"}]),timed[2]))
        conn.execute("UPDATE ai_chat_messages SET event_at=?",(utc(AS_OF-90000),))
    raw=policy(timed)
    raw["rules"][0]["evidence_use"]["predicate"]={"kind":"all_of","terms":[]}
    raw["rules"][0]["evidence_use"]["event_window"]["max_age_seconds"]=172800
    denied=rule("deny-child-health","deny","health")
    denied["evidence_use"]["sources"]["values"]=[source]
    denied["evidence_use"]["tables"]=["signal_objects"]
    denied["evidence_use"]["event_window"]["max_age_seconds"]=window
    raw["rules"].append(denied)
    supplied=bundle(timed,transform=lambda items:[item.model_copy(update={"domains":["health"]})
        if item.evidence.identity.record_id==child["object_id"] else item for item in items])
    assert evaluate(timed,raw,supplied=supplied).verdict==expected


@pytest.mark.parametrize("where", ["input","output"])
def test_exclusion_on_either_evidence_or_output_dominates_permit(timed,where):
    raw=policy(timed)
    raw["rules"][0]["evidence_use"]["predicate"]={"kind":"all_of","terms":[]}
    raw["rules"][0]["release"]["predicate"]={"kind":"all_of","terms":[]}
    raw["rules"].append(rule("deny-health","deny","health"))
    transform=(lambda items:[item.model_copy(update={"domains":["health"]}) for item in items]) if where=="input" else None
    supplied=bundle(timed,transform=transform,output_domain="health" if where=="output" else "reading")
    result=evaluate(timed,raw,supplied=supplied)
    assert result.verdict=="deny" and result.matched_deny_clause_ids==["deny-health"]


def test_unknown_potential_deny_is_not_treated_as_false(timed):
    raw=policy(timed)
    raw["rules"].append(rule("deny-reading","deny"))
    edit(timed,"UPDATE conversation_messages SET event_at=NULL")
    assert evaluate(timed,raw).verdict=="indeterminate"


@pytest.mark.parametrize("axis", ["sources","tables","forms"])
def test_empty_deny_selection_does_not_claim_to_match(timed,axis):
    raw=policy(timed)
    denied=rule("deny-reading","deny")
    if axis=="sources": denied["evidence_use"][axis]["values"]=[]
    elif axis=="tables": denied["evidence_use"][axis]=[]
    else: denied["release"][axis]=[]
    raw["rules"].append(denied)
    assert evaluate(timed,raw).verdict=="permit"


def test_inference_cannot_neutralize_an_explicit_deny(timed):
    raw=policy(timed)
    denied=rule("deny-reading","deny")
    denied["release"]["ceiling"]="inference"
    raw["rules"].append(denied)
    assert evaluate(timed,raw).matched_deny_clause_ids==["deny-reading"]


def test_known_deny_dominates_unknown_fact_validity(timed):
    edit(timed,"UPDATE signal_objects SET valid_from='' ")
    raw=policy(timed)
    raw["rules"].append(rule("deny-reading","deny"))
    result=evaluate(timed,raw)
    assert result.verdict=="deny" and result.matched_deny_clause_ids==["deny-reading"]


def test_allow_input_and_output_predicates_cannot_be_stitched(timed):
    raw=policy(timed)
    first,second=rule("input-reading"),rule("output-reading","permit","health")
    first["release"]["predicate"]=atom("health")
    second["release"]["predicate"]=atom("reading")
    raw["rules"]=[first,second]
    assert evaluate(timed,raw).verdict=="deny"


def test_evaluation_identity_binds_output_review_and_signed_anchor(timed):
    supplied=bundle(timed)
    first=evaluate(timed,supplied=supplied)
    later=evaluate(timed,supplied=supplied,request_as_of=AS_OF+1,now=AS_OF+1)
    assert first.candidate_revision!=later.candidate_revision
    supplied["projection"]=supplied["projection"].model_copy(update={"output_review_revision":"f"*64})
    another=evaluate(timed,supplied=supplied)
    assert another.candidate_revision!=first.candidate_revision


@pytest.mark.parametrize("change", ["row","missing_row","extra_row","lineage","projection","sensitivity"])
def test_stale_or_inconsistent_bundle_cannot_produce_permit(timed,change):
    supplied=bundle(timed)
    if change=="row": next(iter(supplied["rows"].values()))["valid_from"]="changed"
    elif change=="missing_row": supplied["rows"].pop(next(iter(supplied["rows"])))
    elif change=="extra_row": supplied["rows"]["unreviewed"]={"content":"private"}
    elif change=="lineage":
        supplied["evidence"]=supplied["evidence"].model_copy(update={"snapshot":supplied["evidence"].snapshot.model_copy(update={"lineage_revision":"f"*64})})
    elif change=="projection":
        supplied["projection"]=supplied["projection"].model_copy(update={"candidate":supplied["projection"].candidate.model_copy(update={"evidence_review_revision":"f"*64})})
    else:
        supplied["projection"]=supplied["projection"].model_copy(update={"classification":supplied["projection"].classification.model_copy(update={"sensitivity":"none"})})
    with pytest.raises((PolicyError,ValueError)):
        evaluate(timed,supplied=supplied)
