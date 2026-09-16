"""Owner-shadow A/B bridge for P2a raw source release; real scratch services, fake transports only.

Arm A must be the decision the signed `SourceMessageRelease` checkpoints, so the
parity cases drive the real adapter (ledger, signed envelope, recipient dispatch)
and read its receipt. The prose arm is exercised for orchestration, prompt
hygiene, the sibling-fact floor, requalification, cache isolation and retention.
Nothing here measures classifier accuracy, and no serving path can reach the bridge.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import stat

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, edit, owner, payload as change_fact  # noqa: F401 (fixture)
from tests.permissions_v2.test_fact_bridge import EXPERIMENTS, _imported_modules
from tests.permissions_v2.test_release import dispatch, issue, release_setup  # noqa: F401 (fixture)
from tests.permissions_v2.test_source_release_sibling_facts import sibling
from topos.features.lifecycle.record_protection import RecordProtectionStore
from topos.permissions_v2 import release
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import VIEW, Binding
from topos.permissions_v2.evidence import ReviewedClassification
from topos.permissions_v2.identity import ATTESTED_CONTRACT
from topos.permissions_v2.experiments.evaluators import ModelResponse
from topos.permissions_v2.experiments.fact_bridge import ShadowDecisionCache
from topos.permissions_v2.experiments.retention import SyntheticBodyRetention
from topos.permissions_v2.experiments.source_bridge import (SOURCE_PROMPT_REVISION, SOURCE_SYSTEM_PROMPT, VERSION,
    SourceExperimentCapsule, SourceShadowBridge, SourceShadowDecisionCache)

MESSAGE = "I enjoy reading history books."
AI_MESSAGE = "I attend my reading group."
NOW = 1101
FIELDS = ("stage", "verdict", "reason_code", "policy_hash", "candidate_revision", "matched_allow_clause_ids",
          "matched_deny_clause_ids", "required_projection_id", "missing_context_codes")
BOTH_LEAVES = [{"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1", "record_id": "message-1"},
               {"table": "ai_chat_messages", "source_id": "ai-source-1", "record_id": "ai-message-1"}]


# --- fixtures and fakes --------------------------------------------------------

def judgment(**changes):
    return {"verdict": "permit", "matched_allow_clause_ids": ["rule-A"], "matched_deny_clause_ids": [],
            "required_projection_id": VIEW, "missing_context_codes": [], **changes}


class Model:
    """Fake local transport: fixed answers, an optional mid-call hook, never a classifier."""
    def __init__(self, answers=None, callback=None, delay=0, model_revision=None):
        self.answers = answers or [judgment(), judgment()]
        self.calls, self.callback, self.delay, self.model_revision = [], callback, delay, model_revision

    async def complete(self, request):
        self.calls.append(request)
        if self.callback:
            self.callback(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        body = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(body, BaseException):
            raise body
        if body is None:
            return {"body": "not a ModelResponse"}
        return ModelResponse(arm=request.arm, model_id=request.model_id,
            model_revision=self.model_revision or request.model_revision, prompt_revision=request.prompt_revision,
            body=body if isinstance(body, str) else json.dumps(body))


def approved(call):
    return json.loads(call.system.split("\nAPPROVED_POLICY_JSON\n", 1)[1])


def units(call):
    return json.loads(call.candidate_data)["untrusted_candidate_data"]


def deny(policy, rule_id="deny-health", domain="health", sources=None):
    rule = deepcopy(policy["rules"][0])
    rule.update(rule_id=rule_id, effect="deny")
    atom = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": [domain]}
    rule["evidence_use"]["predicate"], rule["release"]["predicate"] = atom, deepcopy(atom)
    if sources is not None:
        rule["evidence_use"]["sources"]["values"] = sources
    policy["rules"].append(rule)
    return policy


def cover_both(policy):
    rule = policy["rules"][0]
    rule["evidence_use"]["sources"]["values"] = ["source-1", "ai-source-1"]
    rule["release"]["forms"][0]["tables"] = ["conversation_messages", "ai_chat_messages"]


def two_leaves(setup):
    edit(setup[5], "UPDATE signal_objects SET source_refs_json=?", (json.dumps(BOTH_LEAVES),))
    attest(setup[5], review_id="review-2")


def changed(setup, *changes):
    raw = deepcopy(setup[1])
    for change in changes:
        change(raw)
    return raw


def prose(raw):
    permits = [rule["rule_id"] for rule in raw["rules"] if rule["effect"] == "permit"]
    denies = [rule["rule_id"] for rule in raw["rules"] if rule["effect"] == "deny"]
    texts = {"rule-A": "My messages about book reading."}
    return {"original": "Share my messages about book reading. Exclude private health information and private reasons for reading.",
        "inclusions": [{"clause_id": clause, "text": texts.get(clause, "My messages about reading, stated again.")} for clause in permits],
        "exclusions": [{"clause_id": clause, "text": "Private health information and private reasons for reading."} for clause in denies],
        "examples": [{"example_id": "ex-permit", "clause_id": "rule-A", "verdict": "permit", "role": "illustration",
                      "text": "I finished a novel set in Fabrikam Heights."}] if "rule-A" in permits else []}


def capsule(raw, *, prose_body=None, prompt_revision=SOURCE_PROMPT_REVISION, processor=None, edit_after=None,
            experiment_id="source-bridge-test"):
    body = {"version": VERSION, "experiment_id": experiment_id, "policy": raw, "prose": prose_body or prose(raw),
        "processor": {"processor": "owner-engine-local", "model_id": "synthetic-local-model", "model_revision": "7" * 64,
            "prompt_revision": prompt_revision, "timeout_ms": 500, "max_prompt_bytes": 32768, "max_response_bytes": 4096,
            "max_output_tokens": 256, "temperature": 0, **(processor or {})}}
    body["owner_approved_revision"] = digest(body)
    if edit_after:
        edit_after(body)
    return SourceExperimentCapsule.parse(body)


def bridge(setup, *, raw=None, transport=None, clock=None, cache=None, retention=None, binding=None, **capsule_options):
    raw = raw if raw is not None else setup[1]
    resolver, reviews, _ = setup[5]
    return SourceShadowBridge(capsule(raw, **capsule_options), resolver=resolver, reviews=reviews,
        binding=Binding.parse(binding or raw["binding"]), clock=clock or (lambda: NOW), transport=transport,
        cache=cache, retention=retention)


def serve(setup, *changes):
    """Drive the signed adapter once: (sent disclosure, refusal code, checkpointed decision)."""
    def change(policy):
        for item in changes:
            item(policy)
    envelope, payload = issue(setup, policy_change=change)
    sent, code = [], None
    try:
        dispatch(setup, envelope, payload, send=lambda result, output: sent.append(output))
    except PolicyError as exc:
        code = exc.code
    with sqlite3.connect(setup[0].protocol.ledger.path) as conn:
        rows = conn.execute("SELECT decision_json FROM p2a_receipts").fetchall()
    assert len(rows) <= 1 and len(sent) <= 1
    return (sent[0] if sent else None), code, (json.loads(rows[0][0]) if rows else None)


def unknown_domain(item):
    # Today every stored review label is a list, so an unknown classification
    # cannot come from a review; both paths read this one label map.
    return {"domain": None, "actor_role": ["authored"], "subject": ["owner"], "sensitivity": [item.sensitivity]}


def always_true(policy):
    for rule in policy["rules"]:
        if rule["effect"] == "permit":
            rule["evidence_use"]["predicate"] = rule["release"]["predicate"] = {"kind": "all_of", "terms": []}


# --- arm A is the serving decision ------------------------------------------------

PARITY = {
    "permit": ((), False, "permit", "rule_permit"),
    "rule_deny": ((lambda p: deny(p, "deny-reading", "reading"),), False, "deny", "rule_deny"),
    "release_predicate_mismatch": ((lambda p: p["rules"][0]["release"]["predicate"].update(values=["health"]),), False, "deny", "rule_deny"),
    "summary_ceiling": ((lambda p: p["rules"][0]["release"].update(ceiling="summary"),), False, "deny", "rule_deny"),
    "unknown_classification": ((), True, "indeterminate", "unknown_context"),
    "unknown_exclusion_over_a_permit": ((always_true, lambda p: deny(p)), True, "indeterminate", "unknown_context"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(PARITY))
async def test_rules_arm_is_the_decision_the_signed_release_checkpoints(release_setup, monkeypatch, case):
    changes, unknown, verdict, reason = PARITY[case]
    if unknown:
        monkeypatch.setattr(release, "_attributes", unknown_domain)
    output, code, served = serve(release_setup, *changes)
    assert (served["verdict"], served["reason_code"]) == (verdict, reason)
    assert (code, output is None) == ((None, False) if verdict == "permit" else ("permission_denied", True))
    result = await bridge(release_setup, raw=changed(release_setup, *changes)).run(release_setup[5][2], arm="rules_v2")
    [decision] = result.stages
    assert {field: getattr(decision, field) for field in FIELDS} == {field: served[field] for field in FIELDS}
    assert decision.arm == "rules_v2" and decision.withheld_code is None and decision.bundle_revision
    assert (result.model_calls, result.observation_state) == (0, "captured_under_gates")
    assert result.execution_enabled is False and result.serving_adapter is None
    assert MESSAGE not in result.model_dump_json()


@pytest.mark.asyncio
async def test_prose_arm_judges_exactly_the_records_the_adapter_releases(release_setup):
    two_leaves(release_setup)
    output, code, served = serve(release_setup, cover_both)
    assert code is None and served["verdict"] == "permit" and len(output["records"]) == 2
    model = Model()
    result = await bridge(release_setup, raw=changed(release_setup, cover_both), transport=model).run(
        release_setup[5][2], arm="semantic_v1")
    assert (result.verdict, [stage.reason_code for stage in result.stages]) == ("permit", ["semantic_permit", "semantic_permit"])
    assert (result.observation_state, result.model_calls) == ("requalified", 2)
    evidence_call, output_call = model.calls
    assert (evidence_call.stage, output_call.stage) == ("evidence_use", "output_release")
    released = [(record["canonical_table"], record["source_id"], record["content"]) for record in output["records"]]
    assert [(unit["table"], unit["source_id"], unit["text"]) for unit in units(output_call)] == released
    evidence = units(evidence_call)
    # The locator shows its predicate only; the messages are judged at both stages.
    assert (evidence[0]["table"], evidence[0]["source_id"], json.loads(evidence[0]["text"])) == ("signal_objects", None, {"predicate": "prefers"})
    assert [(unit["table"], unit["source_id"], unit["text"]) for unit in evidence[1:]] == released
    assert {record[2] for record in released} == {MESSAGE, AI_MESSAGE}
    for stage in result.stages:
        assert (stage.policy_hash, stage.candidate_revision) == (served["policy_hash"], served["candidate_revision"])
        assert stage.required_projection_id == VIEW == served["required_projection_id"]
    dumped = result.model_dump_json()
    assert MESSAGE not in dumped and AI_MESSAGE not in dumped and "APPROVED_POLICY_JSON" not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["permit", "rule_deny"])
async def test_an_oversized_disclosure_withholds_arm_a_as_the_adapter_refuses_it(release_setup, case):
    # Escaped to six canonical bytes a character, one message exceeds the disclosure budget.
    edit(release_setup[5], "UPDATE conversation_messages SET content=?", ("é" * 50000,))
    attest(release_setup[5], review_id="review-2")
    changes = () if case == "permit" else (lambda p: deny(p, "deny-reading", "reading"),)
    output, code, served = serve(release_setup, *changes)
    model = Model()
    rules = await bridge(release_setup, raw=changed(release_setup, *changes)).run(release_setup[5][2], arm="rules_v2")
    semantic = await bridge(release_setup, raw=changed(release_setup, *changes), transport=model).run(
        release_setup[5][2], arm="semantic_v1")
    if case == "rule_deny":
        # The adapter checkpoints a deny before it ever builds the disclosure,
        # so the budget must not turn arm A's deny into a withheld read.
        assert (output, code, served["verdict"]) == (None, "permission_denied", "deny")
        [decision] = rules.stages
        assert {field: getattr(decision, field) for field in FIELDS} == {field: served[field] for field in FIELDS}
        assert decision.withheld_code is None and model.calls == []
        return
    assert (output, code, served) == (None, "disclosure_budget", None)
    assert [(stage.verdict, stage.reason_code, stage.withheld_code) for stage in rules.stages] == [
        ("indeterminate", "evidence_withheld", "disclosure_budget")]
    # Arm B stops before any call; the message is over its per-unit surface budget first.
    assert [(stage.verdict, stage.withheld_code) for stage in semantic.stages] == [("indeterminate", "surface_budget")]
    assert model.calls == [] and semantic.model_calls == 0


# --- the sibling-fact floor ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("disclosure", ["owner_only", "scoped"])
async def test_a_sibling_fact_the_owner_kept_private_withholds_both_arms_before_any_model_call(release_setup, disclosure):
    sibling(release_setup, disclosure=disclosure)
    output, code, served = serve(release_setup)
    model = Model()
    results = {arm: await bridge(release_setup, transport=model).run(release_setup[5][2], arm=arm)
               for arm in ("rules_v2", "semantic_v1")}
    if disclosure == "scoped":
        # Control: a scoped sibling does not withhold, so the floor is what stops the private one.
        assert (code, served["verdict"]) == (None, "permit")
        assert results["semantic_v1"].verdict == "permit" and len(model.calls) == 2
        return
    assert (output, code, served) == (None, "owner_only", None)
    assert model.calls == []
    for arm, result in results.items():
        [decision] = result.stages
        assert (decision.verdict, decision.reason_code, decision.withheld_code) == ("deny", "evidence_withheld", "owner_only"), arm
        assert (result.model_calls, decision.bundle_revision) == (0, None)
    # A read without the source-release floor (the scalar family's) still qualifies this fact.
    assert release_setup[5][0].qualify(release_setup[5][2], reviews=release_setup[5][1]).verdict == "qualified"


@pytest.mark.asyncio
async def test_the_capture_reads_under_the_frozen_legacy_subject_rule_the_adapter_uses(release_setup):
    corpus = release_setup[5]
    # With one self row its entity id is an owner subject under the legacy rule
    # only; the attested rule withholds it until the owner attests that entity.
    change_fact(corpus, subject_entity_id="owner-entity")
    attest(corpus, review_id="review-2")
    assert corpus[0].qualify(corpus[2], reviews=corpus[1], contract=ATTESTED_CONTRACT).verdict == "withheld"
    output, code, served = serve(release_setup)
    assert code is None and served["verdict"] == "permit" and output is not None
    rules = await bridge(release_setup).run(corpus[2], arm="rules_v2")
    [decision] = rules.stages
    assert {field: getattr(decision, field) for field in FIELDS} == {field: served[field] for field in FIELDS}
    model = Model()
    semantic = await bridge(release_setup, transport=model).run(corpus[2], arm="semantic_v1")
    assert semantic.verdict == "permit" and len(model.calls) == 2
    # The locator's subject is now a real entity id, and it still never reaches the model.
    assert all("owner-entity" not in call.system + call.candidate_data for call in model.calls)


# --- arm B -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_label_owner_only_flag_identifier_revision_or_authority_reaches_the_model(release_setup):
    two_leaves(release_setup)
    raw = changed(release_setup, cover_both, lambda p: deny(p, sources=["ai-source-1"]))
    model = Model()
    result = await bridge(release_setup, raw=raw, transport=model).run(release_setup[5][2], arm="semantic_v1")
    assert result.verdict == "permit" and len(model.calls) == 2
    forbidden = [release_setup[5][2], "message-1", "dataset-1", "owner-entity", "ai-conversation-1", "record_id", "dataset_id",
        "entity_id", "domains", "sensitivity", "personal", "authorship", "owner_authored", "direct_self_statement",
        "independent_copies", "none_known", "owner_only", "owner-only", "scoped", '"disclosure"', "is_from_self", "sender_type",
        "review", "revision", "protection", "grant", "actor-1", "client-1", "assignment-1", "owner-1", "node-1", "resource-1",
        "permissions-beta-test", "policy-1", "sources-1", "hard_rules", "validity", '"self"']
    for call in model.calls:
        text = call.system + call.candidate_data
        assert [item for item in forbidden if item in text] == [], call.stage
        assert re.search(r"[0-9a-f]{64}", text) is None, call.stage
        assert all(set(unit) == {"unit_id", "table", "source_id", "text"} for unit in units(call))
        [exclusion] = approved(call)["owner_approved_prose"]["exclusions"]
        assert set(exclusion) == {"clause_id", "text", "sources", "tables", "structural_scope_unit_ids"}
        assert (exclusion["sources"], exclusion["tables"]) == (["ai-source-1"], ["ai_chat_messages", "conversation_messages"])
        assert "That list is scope, not a match" in call.system and "unit_ids" not in call.system.replace("structural_scope_unit_ids", "")
        assert approved(call)["form"] == VIEW and approved(call)["owner_approved_prose"]["original"] is not None
    evidence_call, output_call = model.calls
    # One reached message brings the whole derivation into evidence scope, but only
    # the reached record into output scope, exactly as the rules evaluate it.
    [evidence_exclusion] = approved(evidence_call)["owner_approved_prose"]["exclusions"]
    assert evidence_exclusion["structural_scope_unit_ids"] == [unit["unit_id"] for unit in units(evidence_call)] == ["u1", "u2", "u3"]
    [output_exclusion] = approved(output_call)["owner_approved_prose"]["exclusions"]
    tables = {unit["unit_id"]: unit["table"] for unit in units(output_call)}
    assert [tables[unit] for unit in output_exclusion["structural_scope_unit_ids"]] == ["ai_chat_messages"]


COVERAGE = {
    "neither": (),
    "one_source": (lambda p: p["rules"][0]["release"]["forms"][0].update(tables=["conversation_messages", "ai_chat_messages"]),),
    "one_table": (lambda p: p["rules"][0]["evidence_use"]["sources"].update(values=["source-1", "ai-source-1"]),),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(COVERAGE))
async def test_an_inclusion_is_offered_only_when_it_covers_every_message_released_together(release_setup, case):
    # Messages go out whole and together, so an inclusion that reaches one of
    # two messages must never be offered, just as the rules never apply it.
    two_leaves(release_setup)
    output, code, served = serve(release_setup, *COVERAGE[case])
    assert (output, code, served["verdict"], served["reason_code"]) == (None, "permission_denied", "deny", "rule_deny")
    raw = changed(release_setup, *COVERAGE[case])
    rules = await bridge(release_setup, raw=raw).run(release_setup[5][2], arm="rules_v2")
    assert {field: getattr(rules.stages[0], field) for field in FIELDS} == {field: served[field] for field in FIELDS}
    model = Model()
    result = await bridge(release_setup, raw=raw, transport=model).run(release_setup[5][2], arm="semantic_v1")
    assert [(stage.verdict, stage.reason_code) for stage in result.stages] == [("deny", "no_structural_match")]
    assert model.calls == [] and result.model_calls == 0


@pytest.mark.asyncio
async def test_only_exclusions_whose_processor_sources_and_tables_reach_a_message_are_offered(release_setup):
    two_leaves(release_setup)

    def exclusions(policy):
        cover_both(policy)
        deny(policy, "deny-reached", sources=["ai-source-1"])
        # Its source reaches the AI message, but its table does not.
        deny(policy, "deny-other-table", sources=["ai-source-1"])
        policy["rules"][-1]["release"]["forms"][0]["tables"] = ["conversation_messages"]
        # Reaches both messages, but not through the owner's local processor.
        deny(policy, "deny-remote", sources=["source-1", "ai-source-1"])
        policy["rules"][-1]["evidence_use"]["processors"]["values"] = []

    fact_id = release_setup[5][2]
    output, code, served = serve(release_setup, exclusions)
    assert code is None and served["verdict"] == "permit"
    raw = changed(release_setup, exclusions)
    rules = await bridge(release_setup, raw=raw).run(fact_id, arm="rules_v2")
    assert {field: getattr(rules.stages[0], field) for field in FIELDS} == {field: served[field] for field in FIELDS}
    twin = prose(raw)
    twin["examples"].append({"example_id": "ex-remote", "clause_id": "deny-remote", "verdict": "deny",
                             "role": "illustration", "text": "A note about a clinic visit in Contoso City."})
    model = Model()
    result = await bridge(release_setup, raw=raw, transport=model, prose_body=twin).run(fact_id, arm="semantic_v1")
    assert result.verdict == "permit" and len(model.calls) == 2
    for call in model.calls:
        offered = approved(call)["owner_approved_prose"]
        assert [item["clause_id"] for item in offered["exclusions"]] == ["deny-reached"], call.stage
        # Neither the original nor an example may restate a clause that was not offered.
        assert offered["original"] is None and "Contoso City" not in call.system
    for name in ("deny-other-table", "deny-remote"):
        # A real deny rule the structure did not offer is still an unbound clause.
        model = Model([judgment(verdict="deny", matched_allow_clause_ids=[], matched_deny_clause_ids=[name])])
        result = await bridge(release_setup, raw=raw, transport=model, prose_body=twin).run(fact_id, arm="semantic_v1")
        assert (result.verdict, result.stages[-1].reason_code) == ("indeterminate", "clause_binding"), name


@pytest.mark.asyncio
async def test_a_disclosure_over_budget_stops_the_prose_arm_although_each_message_fits_its_surface(release_setup):
    two_leaves(release_setup)
    # An astral character escapes to twelve canonical bytes, so two messages at
    # the surface limit fit arm B's per-unit budget but not the disclosure budget.
    # Distinct texts: identical messages would be withheld as independent copies.
    for table, character in (("conversation_messages", "\U0001F4DA"), ("ai_chat_messages", "\U0001F4D6")):
        edit(release_setup[5], f"UPDATE {table} SET content=?", (character * 16000,))
    attest(release_setup[5], review_id="review-3")
    output, code, served = serve(release_setup, cover_both)
    assert (output, code, served) == (None, "disclosure_budget", None)
    raw, model = changed(release_setup, cover_both), Model()
    rules = await bridge(release_setup, raw=raw).run(release_setup[5][2], arm="rules_v2")
    semantic = await bridge(release_setup, raw=raw, transport=model).run(release_setup[5][2], arm="semantic_v1")
    assert [(stage.stage, stage.verdict, stage.withheld_code) for stage in rules.stages] == [
        ("output_release", "indeterminate", "disclosure_budget")]
    assert [(stage.stage, stage.verdict, stage.reason_code, stage.withheld_code) for stage in semantic.stages] == [
        ("output_release", "indeterminate", "evidence_withheld", "disclosure_budget")]
    assert model.calls == [] and semantic.model_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,verdict,reason", [
    (judgment(matched_deny_clause_ids=["deny-health"]), "deny", "semantic_deny"),
    (judgment(verdict="deny", matched_allow_clause_ids=[]), "deny", "no_semantic_match"),
    (judgment(verdict="deny"), "indeterminate", "clause_binding"),
    (judgment(matched_allow_clause_ids=["invented"]), "indeterminate", "clause_binding"),
    (judgment(verdict="deny", matched_allow_clause_ids=[], matched_deny_clause_ids=["invented-exclusion"]), "indeterminate", "clause_binding"),
    (judgment(required_projection_id=None), "indeterminate", "projection_required"),
    (judgment(required_projection_id="owner_stated_fact.scalar.v1"), "indeterminate", "malformed_decision"),
    (judgment(matched_allow_clause_ids=[]), "indeterminate", "clause_binding"),
    (judgment(missing_context_codes=["context"]), "indeterminate", "unknown_context"),
    (judgment(verdict="indeterminate", matched_allow_clause_ids=[]), "indeterminate", "unknown_context"),
    ("{not json", "indeterminate", "malformed_decision"),
    (json.dumps({**judgment(), "explanation": "extra"}), "indeterminate", "malformed_decision"),
    (json.dumps({**judgment(), "records": [{"content": "redacted"}]}), "indeterminate", "malformed_decision"),
    (json.dumps(judgment()) + " " * 4096, "indeterminate", "response_budget"),
    (RuntimeError("synthetic provider failure naming Contoso"), "indeterminate", "model_error"),
    (None, "indeterminate", "malformed_decision"),
], ids=["semantic_deny", "no_match", "deny_names_inclusion", "invented_inclusion", "invented_exclusion", "no_projection",
        "wrong_projection", "permit_without_inclusion", "missing_context", "indeterminate", "not_json", "extra_field",
        "redaction_field", "oversized", "provider_error", "not_a_response"])
async def test_exclusions_dominate_and_the_model_cannot_widen_its_answer_or_fall_back_to_rules(release_setup, answer, verdict, reason):
    raw = changed(release_setup, lambda p: deny(p))
    rules = await bridge(release_setup, raw=raw).run(release_setup[5][2], arm="rules_v2")
    assert rules.verdict == "permit"
    model = Model([answer, answer])
    result = await bridge(release_setup, raw=raw, transport=model).run(release_setup[5][2], arm="semantic_v1")
    assert (result.verdict, result.stages[-1].reason_code) == (verdict, reason)
    assert len(result.stages) == len(model.calls) == result.model_calls == 1 and result.observation_state == "requalified"
    assert all(stage.arm == "semantic_v1" and not stage.reason_code.startswith("rule_") for stage in result.stages)
    if reason == "semantic_deny":
        assert result.stages[-1].matched_deny_clause_ids == ["deny-health"]
    assert "Contoso" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason", [("timeout", "model_timeout"), ("identity", "model_identity")])
async def test_a_slow_or_misidentified_model_counts_the_call_and_withholds(release_setup, failure, reason):
    model = Model(delay=0.2) if failure == "timeout" else Model(model_revision="8" * 64)
    result = await bridge(release_setup, transport=model, processor={"timeout_ms": 50}).run(release_setup[5][2], arm="semantic_v1")
    assert (result.verdict, result.stages[-1].reason_code, result.model_calls, len(model.calls)) == ("indeterminate", reason, 1, 1)


@pytest.mark.asyncio
async def test_output_stage_may_only_use_inclusions_that_permitted_the_evidence(release_setup):
    raw = changed(release_setup, lambda p: p["rules"].append({**deepcopy(p["rules"][0]), "rule_id": "allow-second"}))
    model = Model([judgment(), judgment(matched_allow_clause_ids=["allow-second"])])
    result = await bridge(release_setup, raw=raw, transport=model).run(release_setup[5][2], arm="semantic_v1")
    assert approved(model.calls[0])["eligible_inclusion_ids"] == ["rule-A", "allow-second"]
    assert approved(model.calls[1])["eligible_inclusion_ids"] == ["rule-A"]
    assert [stage.reason_code for stage in result.stages] == ["semantic_permit", "clause_binding"]
    # The original restates the narrowed-away inclusion, so only clause texts remain.
    assert approved(model.calls[0])["owner_approved_prose"]["original"] is not None
    assert approved(model.calls[1])["owner_approved_prose"]["original"] is None and "stated again" not in model.calls[1].system


@pytest.mark.asyncio
@pytest.mark.parametrize("case,verdict,reason,code", [
    ("sources_not_covered", "deny", "no_structural_match", None),
    ("tables_not_covered", "deny", "no_structural_match", None),
    ("summary_ceiling", "deny", "no_structural_match", None),
    ("no_local_processor", "deny", "no_structural_match", None),
    ("expired_policy", "deny", "evidence_withheld", "policy_time"),
    ("surface_budget", "indeterminate", "evidence_withheld", "surface_budget"),
    ("unconfigured_model", "indeterminate", "unconfigured_model", None),
    ("unpinned_prompt", "indeterminate", "model_identity", None),
    ("prompt_budget", "indeterminate", "prompt_budget", None),
])
async def test_structural_and_pin_stops_precede_any_model_call(release_setup, case, verdict, reason, code):
    changes = {"sources_not_covered": lambda p: p["rules"][0]["evidence_use"]["sources"].update(values=["ai-source-1"]),
        "tables_not_covered": lambda p: p["rules"][0]["release"]["forms"][0].update(tables=["ai_chat_messages"]),
        "summary_ceiling": lambda p: p["rules"][0]["release"].update(ceiling="summary"),
        "no_local_processor": lambda p: p["rules"][0]["evidence_use"]["processors"].update(values=[])}
    raw = changed(release_setup, *([changes[case]] if case in changes else []))
    if case == "surface_budget":
        edit(release_setup[5], "UPDATE conversation_messages SET content=?", ("Northwind " * 1601,))
        attest(release_setup[5], review_id="review-2")
    model = Model()
    options = {"raw": raw, "transport": None if case == "unconfigured_model" else model,
               "clock": (lambda: 5000) if case == "expired_policy" else None}
    if case == "unpinned_prompt":
        options["prompt_revision"] = "9" * 64
    if case == "prompt_budget":
        options["processor"] = {"max_prompt_bytes": 1024}
    rules = await bridge(release_setup, **options).run(release_setup[5][2], arm="rules_v2")
    result = await bridge(release_setup, **options).run(release_setup[5][2], arm="semantic_v1")
    assert model.calls == [] and result.model_calls == 0 and result.observation_state == "captured_under_gates"
    [decision] = result.stages
    assert (decision.verdict, decision.reason_code, decision.withheld_code) == (verdict, reason, code)
    if case in changes or case == "expired_policy":
        assert rules.verdict == "deny"
    elif case in ("surface_budget", "unconfigured_model", "unpinned_prompt", "prompt_budget"):
        # Arm A stays the serving decision whatever stops the prose arm.
        assert (rules.verdict, rules.stages[0].reason_code) == ("permit", "rule_permit")


# --- requalification, cache and retention ---------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["evidence_use", "output_release"])
@pytest.mark.parametrize("change", ["revoke_review", "owner_only_sibling", "protect_message", "protect_elsewhere",
                                    "edit_message", "expire_policy", "edit_and_review", "relabel_review"])
async def test_a_change_during_either_model_call_is_never_retained_or_cached(release_setup, stage, change):
    corpus, clock, cached_before_call = release_setup[5], [NOW], []

    def mutate(request):
        if request.stage != stage:
            return
        cached_before_call.append(len(cache._entries))
        if change == "revoke_review":
            with owner():
                corpus[1].revoke_review("review-1")
        elif change == "owner_only_sibling":
            sibling(release_setup)
        elif change.startswith("protect"):
            # Signed authority binds the node-wide protection revision, so even a
            # record outside this closure stales what the model was shown.
            table, record = (("conversation_messages", "message-1") if change == "protect_message"
                             else ("ai_chat_messages", "ai-message-1"))
            with owner(), sqlite3.connect(corpus[0].path) as conn:
                RecordProtectionStore(conn).protect(canonical_table=table, record_id=record)
        elif change == "edit_message":
            edit(corpus, "UPDATE conversation_messages SET content='I moved to Lakeview Row.'")
        elif change == "edit_and_review":
            # Reviewed again, the edited closure still qualifies: only the revision
            # comparison can tell that the model was shown other text.
            edit(corpus, "UPDATE conversation_messages SET content='I moved to Lakeview Row.'")
            attest(corpus, review_id="review-2")
        elif change == "relabel_review":
            # Same closure, new labels that arm A would deny on; still qualifies.
            attest(corpus, review_id="review-2", transform=lambda items: [
                ReviewedClassification.parse({**item.model_dump(), "domains": ["health"]}) for item in items])
        else:
            clock[0] = 5000

    cache, model = SourceShadowDecisionCache(), Model(callback=mutate)
    result = await bridge(release_setup, transport=model, clock=lambda: clock[0], cache=cache).run(corpus[2], arm="semantic_v1")
    assert (result.observation_state, result.verdict, result.stages[-1].reason_code) == ("not_retained", "indeterminate", "requalification_failed")
    # A change during stage 1 is caught before output_release sends the records;
    # one during stage 2 is caught before the permit is kept.
    expected = ["evidence_use"] if stage == "evidence_use" else ["evidence_use", "output_release"]
    assert [call.stage for call in model.calls] == expected and result.model_calls == len(expected)
    assert [item.stage for item in result.stages] == expected + [stage]
    # Stage 1's permit was cached before the stage-2 call and is evicted with it.
    assert cached_before_call == [len(expected) - 1] and cache._entries == {}


@pytest.mark.asyncio
async def test_cache_reuses_only_the_identical_capture_and_never_operational_failures(release_setup):
    fact_id, model, cache = release_setup[5][2], Model(), SourceShadowDecisionCache()
    first = await bridge(release_setup, transport=model, cache=cache).run(fact_id, arm="semantic_v1")
    second = await bridge(release_setup, transport=model, cache=cache).run(fact_id, arm="semantic_v1")
    assert first.verdict == second.verdict == "permit" and len(model.calls) == 2
    assert (second.model_calls, second.observation_state, second.stages) == (0, "captured_under_gates", first.stages)
    assert all(MESSAGE.encode() not in body and b"APPROVED_POLICY_JSON" not in body for body in cache._entries.values())
    later = await bridge(release_setup, transport=model, cache=cache, clock=lambda: NOW + 1).run(fact_id, arm="semantic_v1")
    other_capsule = await bridge(release_setup, transport=model, cache=cache, experiment_id="source-bridge-other").run(fact_id, arm="semantic_v1")
    assert later.model_calls == other_capsule.model_calls == 2 and len(model.calls) == 6
    # Same capsule and clock, but the owner edited and reviewed the message again:
    # a decision about the old text is never reused for the new one.
    edit(release_setup[5], "UPDATE conversation_messages SET content='I finished a novel set in Fabrikam Heights.'")
    attest(release_setup[5], review_id="review-2")
    edited = await bridge(release_setup, transport=model, cache=cache).run(fact_id, arm="semantic_v1")
    assert edited.model_calls == 2 and len(model.calls) == 8 and edited.stages[0].bundle_revision != first.stages[0].bundle_revision
    entries = dict(cache._entries)
    await bridge(release_setup, cache=cache).run(fact_id, arm="rules_v2")
    assert cache._entries == entries
    slow = Model(delay=0.2)
    for _ in range(2):
        result = await bridge(release_setup, transport=slow, cache=cache, clock=lambda: NOW + 5,
                              processor={"timeout_ms": 50}).run(fact_id, arm="semantic_v1")
        assert (result.verdict, result.stages[-1].reason_code) == ("indeterminate", "model_timeout")
    assert len(slow.calls) == 2
    # A cache the fact bridge fills is a different grammar and is never accepted here.
    with pytest.raises(PolicyError, match="cache_invalid"):
        bridge(release_setup, cache=ShadowDecisionCache())


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_retention_is_refused_unless_the_run_is_flagged_synthetic(tmp_path, flag):
    with pytest.raises(PolicyError, match="retention_requires_synthetic_run"):
        SyntheticBodyRetention(tmp_path / "bodies", synthetic_run=flag)
    assert not (tmp_path / "bodies").exists()


@pytest.mark.asyncio
async def test_a_synthetic_run_retains_each_exchange_privately_and_results_carry_no_body(release_setup, tmp_path):
    with pytest.raises(PolicyError, match="retention_invalid"):
        bridge(release_setup, retention=str(tmp_path / "bodies"))
    model = Model()
    with SyntheticBodyRetention(tmp_path / "bodies", synthetic_run=True) as retention:
        result = await bridge(release_setup, transport=model, retention=retention).run(release_setup[5][2], arm="semantic_v1")
        stopped = await bridge(release_setup, raw=changed(release_setup, lambda p: p["rules"][0]["release"].update(ceiling="summary")),
                               transport=model, retention=retention).run(release_setup[5][2], arm="semantic_v1")
        files = list(retention.files)
    assert result.verdict == "permit" and stopped.stages[0].reason_code == "no_structural_match"
    # A structural stop makes no call, so nothing more is retained.
    assert len(files) == 2 == len(model.calls)
    records = [json.loads((tmp_path / "bodies" / name).read_text()) for name in files]
    assert [row["request"] for row in records] == [call.model_dump() for call in model.calls]
    assert [row["reason_code"] for row in records] == [stage.reason_code for stage in result.stages]
    assert all(stat.S_IMODE((tmp_path / "bodies" / name).stat().st_mode) == 0o600 for name in files)
    assert MESSAGE in records[1]["request"]["candidate_data"]
    dumped = result.model_dump_json()
    assert MESSAGE not in dumped and "APPROVED_POLICY_JSON" not in dumped


# --- closure of the capsule and the boundary ---------------------------------------------

@pytest.mark.parametrize("damage", ["foreign_inclusion", "missing_exclusion", "example_amends_exclusion", "edited_prose",
                                    "other_vocabulary", "budget"])
def test_capsule_is_closed_and_mirrors_the_policy_rules(release_setup, damage):
    raw = changed(release_setup, lambda p: deny(p))
    twin = prose(raw)
    edit_after = None
    if damage == "foreign_inclusion":
        twin["inclusions"].append({"clause_id": "not-a-rule", "text": "Anything."})
    elif damage == "missing_exclusion":
        twin["exclusions"] = []
    elif damage == "example_amends_exclusion":
        twin["examples"].append({"example_id": "ex-2", "clause_id": "deny-health", "verdict": "permit", "role": "illustration", "text": "x"})
    elif damage == "other_vocabulary":
        raw["versions"]["vocabulary"] = "different-v1"
    elif damage == "edited_prose":
        edit_after = lambda body: body["prose"].update(original="Share every message.")  # noqa: E731
    capsule(changed(release_setup, lambda p: deny(p)))  # the undamaged twin is accepted
    with pytest.raises(PolicyError, match="schema_invalid"):
        capsule(raw, prose_body=twin, edit_after=edit_after, processor={"max_prompt_bytes": 100} if damage == "budget" else None)


@pytest.mark.asyncio
async def test_binding_and_locator_are_enforced_before_any_read(release_setup):
    raw = release_setup[1]
    with pytest.raises(PolicyError, match="source_policy_binding"):
        bridge(release_setup, binding={**raw["binding"], "actor_id": "other"})
    elsewhere = changed(release_setup, lambda p: p["binding"].update(node_id="node-2"))
    with pytest.raises(PolicyError, match="source_policy_binding"):
        bridge(release_setup, raw=elsewhere)
    for fact_id in (release_setup[5][2] + "?mode=owner", release_setup[5][2] + "\n", "", 7):
        with pytest.raises(PolicyError):
            await bridge(release_setup).run(fact_id, arm="rules_v2")
    with pytest.raises(PolicyError, match="arm_invalid"):
        await bridge(release_setup).run(release_setup[5][2], arm="semantic_v2")


def test_prompt_revision_is_pinned_to_its_template_and_version():
    """Any edit to the template must come with a deliberate version bump and a new pin."""
    assert SOURCE_PROMPT_REVISION == digest({"template": SOURCE_SYSTEM_PROMPT, "version": "source-bridge-prompt/v1"})
    assert SOURCE_PROMPT_REVISION == "9b15cb078334584c18f923502df1488a38085b966280b7d25079f3925d9b2664"


SOURCE_BRIDGE = EXPERIMENTS + ".source_bridge"


@pytest.mark.parametrize("source,package", [
    ("from .experiments import source_bridge", "topos.permissions_v2"),
    ("from .experiments.source_bridge import SourceShadowBridge", "topos.permissions_v2"),
    ("from ..permissions_v2.experiments.source_bridge import SourceShadowBridge", "topos.api"),
    ("import topos.permissions_v2.experiments.source_bridge", "topos.api"),
    ("importlib.import_module('topos.permissions_v2.experiments.source_bridge')", "topos.api"),
])
def test_import_boundary_scan_sees_every_spelling_of_the_source_bridge(tmp_path, source, package):
    path = tmp_path / "probe.py"
    path.write_text(source)
    assert SOURCE_BRIDGE in set(_imported_modules(path, package)), source


def test_no_serving_module_imports_the_source_bridge():
    """The fact bridge's scan covers the whole package; this pins the new module inside it."""
    root = Path(__file__).resolve().parents[2]
    experiments = root / "topos" / "permissions_v2" / "experiments"
    assert (experiments / "source_bridge.py").is_file()
    offenders, scanned = [], 0
    for path in sorted((root / "topos").rglob("*.py")):
        if experiments in path.parents or "tests" in path.relative_to(root).parts:
            continue
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        package = module.rsplit(".", 1)[0] if path.name != "__init__.py" else module.removesuffix(".__init__")
        scanned += 1
        if any(name == EXPERIMENTS or name.startswith(EXPERIMENTS + ".") for name in _imported_modules(path, package)):
            offenders.append(module)
    assert scanned > 100 and offenders == []
