"""Owner-shadow A/B bridge over real scratch P2b services; fake transports only.

These establish orchestration, hygiene and requalification behaviour. They do
not measure classifier accuracy, and no serving path can reach this package.
"""
import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, owner, attest, edit, payload
from tests.permissions_v2.test_fact_policy import timed, policy, rule, AS_OF, utc, two_leaves
from tests.permissions_v2.test_projection_reviews import service as projection_service, lookup
from topos.features.lifecycle.record_protection import RecordProtectionStore
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.contract import Binding
from topos.permissions_v2.experiments.evaluators import ModelResponse
from topos.permissions_v2.experiments.fact_bridge import (FACT_PROMPT_REVISION, FACT_SYSTEM_PROMPT, VERSION,
    FactExperimentCapsule, FactShadowBridge, ShadowDecisionCache)
from topos.permissions_v2.fact_contract import VIEW
from topos.permissions_v2.projection_reviews import RecordProjectionReview, RevokeProjectionReview

MESSAGE = "I enjoy reading history books."


def judgment(**changes):
    return {"verdict": "permit", "matched_allow_clause_ids": ["allow-reading"], "matched_deny_clause_ids": [],
            "required_projection_id": VIEW, "missing_context_codes": [], **changes}


class Fake:
    def __init__(self, answers=None, callback=None, delay=0):
        self.answers = answers or [judgment(), judgment()]
        self.calls = []
        self.callback = callback
        self.delay = delay

    async def complete(self, request):
        self.calls.append(request)
        if self.callback:
            self.callback(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        body = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return ModelResponse(arm=request.arm, model_id=request.model_id, model_revision=request.model_revision,
            prompt_revision=request.prompt_revision, body=body if isinstance(body, str) else json.dumps(body))


def approved(call):
    return json.loads(call.system.split("\nAPPROVED_POLICY_JSON\n", 1)[1])


def prose(exclusions=()):
    return {"original": "Share my book reading preferences. Exclude private health information and private reasons for reading.",
        "inclusions": [{"clause_id": "allow-reading", "text": "My book reading preferences."}],
        "exclusions": [{"clause_id": clause, "text": "Private health information and private reasons for reading."} for clause in exclusions],
        "examples": [{"example_id": "ex-permit", "clause_id": "allow-reading", "verdict": "permit", "role": "illustration",
                      "text": "I enjoy reading science fiction."}]}


def capsule(raw, *, prose_body=None, prompt_revision=FACT_PROMPT_REVISION, edit_after=None):
    body = {"version": VERSION, "experiment_id": "bridge-test", "policy": raw,
        "prose": prose_body or prose(clause for clause in (r["rule_id"] for r in raw["rules"] if r["effect"] == "deny")),
        "processor": {"processor": "owner-engine-local", "model_id": "synthetic-local-model", "model_revision": "7" * 64,
            "prompt_revision": prompt_revision, "timeout_ms": 500, "max_prompt_bytes": 32768, "max_response_bytes": 4096,
            "max_output_tokens": 256, "temperature": 0}}
    body["owner_approved_revision"] = digest(body)
    if edit_after:
        edit_after(body)
    return FactExperimentCapsule.parse(body)


def record_output(corpus, service, *, evidence_transform=None, output_domains=("reading",)):
    attest(corpus, transform=evidence_transform)
    with owner():
        preview = service.preview(lookup(corpus), now=1200)
        request = RecordProjectionReview(review_id="output-review-1", expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash, expected_current_review_revision=None,
            classification={"domains": list(output_domains), "sensitivity": "personal", "subject": "self",
                            "assertion": "explicit_atomic_preference"})
        return service.record(request, now=1200)


def bridge(corpus, service, *, raw=None, transport=None, clock=None, cache=None, **capsule_options):
    raw = raw or policy(corpus)
    return FactShadowBridge(capsule(raw, **capsule_options), projections=service, binding=Binding.parse(raw["binding"]),
        clock=clock or (lambda: AS_OF), transport=transport, cache=cache)


async def both(corpus, service, **options):
    results = {}
    for arm in ("rules_v2", "semantic_v1"):
        results[arm] = await bridge(corpus, service, **options).run(corpus[2], request_as_of=AS_OF, arm=arm)
    return results


@pytest.mark.asyncio
async def test_rules_arm_is_the_serving_evaluator_and_results_carry_no_content(timed, projection_service):
    record_output(timed, projection_service)
    result = await bridge(timed, projection_service).run(timed[2], request_as_of=AS_OF, arm="rules_v2")
    [decision] = result.stages
    assert (decision.verdict, decision.reason_code, decision.matched_allow_clause_ids) == ("permit", "rule_permit", ["allow-reading"])
    assert decision.required_projection_id == VIEW and decision.stage == "output_release"
    assert result.model_calls == 0 and result.observation_state == "captured_under_gates"
    assert result.execution_enabled is False and result.serving_adapter is None
    dumped = result.model_dump_json()
    assert "history books" not in dumped and MESSAGE not in dumped and "I enjoy" not in dumped


@pytest.mark.asyncio
async def test_semantic_arm_runs_two_stages_with_prose_only_prompts_and_requalifies(timed, projection_service):
    record_output(timed, projection_service)
    model = Fake()
    result = await bridge(timed, projection_service, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert result.verdict == "permit" and [stage.stage for stage in result.stages] == ["evidence_use", "output_release"]
    assert result.observation_state == "requalified" and result.model_calls == 2
    evidence_call, output_call = model.calls
    evidence_units = json.loads(evidence_call.candidate_data)["untrusted_candidate_data"]
    assert [unit["table"] for unit in evidence_units] == ["signal_objects", "conversation_messages"]
    # The derived fact names its predicate only: no subject id, and not the value
    # the output stage is about to inspect.
    assert json.loads(evidence_units[0]["text"]) == {"predicate": "prefers"}
    assert evidence_units[1]["text"] == MESSAGE and set(evidence_units[1]) == {"unit_id", "table", "source_id", "dataset_id", "text"}
    output_units = json.loads(output_call.candidate_data)["untrusted_candidate_data"]
    assert [unit["unit_id"] for unit in output_units] == ["output"] and MESSAGE not in output_call.candidate_data
    assert json.loads(output_units[0]["text"]) == {"subject": "self", "predicate": "prefers", "value": "history books"}
    for call in model.calls:
        for hidden in ("sensitivity", "authorship", "owner_authored", "domains", "review", "revision", "protection", "grant-1", "actor-1"):
            assert hidden not in call.candidate_data
        assert "My book reading preferences." in call.system and '"eligible_inclusion_ids":["allow-reading"]' in call.system
        assert "actor-1" not in call.system and "personal" not in call.system
    dumped = result.model_dump_json()
    assert "history books" not in dumped and MESSAGE not in dumped
    assert result.stages[1].reason_code == "semantic_permit" and result.stages[1].required_projection_id == VIEW


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["future_event", "stale_event", "unknown_validity", "expired_policy", "no_source_coverage", "edited_after_review"])
async def test_structural_floor_stops_both_arms_before_any_model_call(timed, projection_service, case):
    raw = policy(timed)
    if case == "expired_policy":
        raw["validity"]["expires_at"] = AS_OF
    elif case == "no_source_coverage":
        raw["rules"][0]["evidence_use"]["sources"]["values"] = ["ai-source-1"]
    # Rows change before the owner reviews them; a later change makes the
    # review itself stale, which the services withhold before any structure.
    if case == "future_event":
        edit(timed, "UPDATE conversation_messages SET event_at=?", (utc(AS_OF + 1),))
    elif case == "stale_event":
        edit(timed, "UPDATE conversation_messages SET event_at=?", (utc(AS_OF - 200000),))
    elif case == "unknown_validity":
        edit(timed, "UPDATE signal_objects SET valid_from=''")
    record_output(timed, projection_service)
    if case == "edited_after_review":
        edit(timed, "UPDATE conversation_messages SET event_at=?", (utc(AS_OF - 200000),))
    model = Fake()
    results = await both(timed, projection_service, raw=raw, transport=model)
    assert model.calls == [] and results["semantic_v1"].model_calls == 0
    assert results["semantic_v1"].verdict == results["rules_v2"].verdict != "permit"
    expected = {"future_event": ("indeterminate", "unknown_context"),
        "stale_event": ("deny", "no_structural_match"), "unknown_validity": ("indeterminate", "unknown_context"),
        "expired_policy": ("deny", "stale_authority"), "no_source_coverage": ("deny", "no_structural_match"),
        "edited_after_review": ("indeterminate", "evidence_withheld")}[case]
    semantic = results["semantic_v1"].stages[-1]
    assert (semantic.verdict, semantic.reason_code) == expected
    if case in ("future_event", "unknown_validity"):
        assert semantic.missing_context_codes == (["time"] if case == "future_event" else ["fact_validity"])
    if case == "edited_after_review":
        assert semantic.withheld_code == "review_stale" == results["rules_v2"].stages[-1].withheld_code


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,verdict,reason", [
    (judgment(matched_deny_clause_ids=["deny-health"]), "deny", "semantic_deny"),
    (judgment(verdict="deny", matched_allow_clause_ids=[]), "deny", "no_semantic_match"),
    (judgment(verdict="deny"), "indeterminate", "clause_binding"),
    (judgment(matched_allow_clause_ids=["invented"]), "indeterminate", "clause_binding"),
    (judgment(required_projection_id=None), "indeterminate", "projection_required"),
    (judgment(matched_allow_clause_ids=[]), "indeterminate", "clause_binding"),
    (judgment(missing_context_codes=["context"]), "indeterminate", "unknown_context"),
    ("{not json", "indeterminate", "malformed_decision"),
    (json.dumps({**judgment(), "explanation": "extra"}), "indeterminate", "malformed_decision"),
    (json.dumps({**judgment(), "verdict": "permit", "output": "leak"}), "indeterminate", "malformed_decision"),
])
async def test_exclusions_dominate_and_model_cannot_widen_its_answer(timed, projection_service, answer, verdict, reason):
    raw = policy(timed)
    raw["rules"].append(rule("deny-health", "deny", "health"))
    record_output(timed, projection_service)
    model = Fake([answer, answer])
    result = await bridge(timed, projection_service, raw=raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert (result.verdict, result.stages[-1].reason_code) == (verdict, reason)
    assert len(result.stages) == 1 and result.observation_state == "requalified"


@pytest.mark.asyncio
async def test_output_stage_may_only_use_inclusions_that_permitted_the_evidence(timed, projection_service):
    raw = policy(timed)
    raw["rules"].append(rule("allow-second"))
    record_output(timed, projection_service)
    twin = prose()
    twin["inclusions"].append({"clause_id": "allow-second", "text": "My reading preferences, stated again."})
    model = Fake([judgment(), judgment(matched_allow_clause_ids=["allow-second"])])
    result = await bridge(timed, projection_service, raw=raw, transport=model, prose_body=twin).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert '"eligible_inclusion_ids":["allow-reading","allow-second"]' in model.calls[0].system
    assert '"eligible_inclusion_ids":["allow-reading"]' in model.calls[1].system
    assert result.stages[1].reason_code == "clause_binding" and result.verdict == "indeterminate"
    # The original restates the narrowed-away inclusion, so only clause texts remain.
    assert approved(model.calls[0])["owner_approved_prose"]["original"] is not None
    output_prose = approved(model.calls[1])["owner_approved_prose"]
    assert output_prose["original"] is None and "stated again" not in model.calls[1].system


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_window,offered,verdict,reason", [
    (86400, False, "indeterminate", "clause_binding"), (172800, True, "deny", "semantic_deny")])
async def test_exclusion_window_masks_gate_what_the_model_may_match(timed, projection_service, deny_window, offered, verdict, reason):
    two_leaves(timed)
    edit(timed, "UPDATE ai_chat_messages SET event_at=?", (utc(AS_OF - 100000),))
    raw = policy(timed)
    raw["rules"][0]["evidence_use"]["event_window"]["max_age_seconds"] = 172800
    denied = rule("deny-health", "deny", "health")
    denied["evidence_use"]["sources"]["values"] = ["ai-source-1"]
    denied["evidence_use"]["event_window"]["max_age_seconds"] = deny_window
    raw["rules"].append(denied)
    record_output(timed, projection_service)
    model = Fake([judgment(matched_deny_clause_ids=["deny-health"])])
    result = await bridge(timed, projection_service, raw=raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    # An exclusion outside its own window is never offered, so the model
    # cannot match it; inside the window a match dominates the permit.
    assert ("deny-health" in model.calls[0].system) is offered
    # Neither the exclusion's own text nor the original prose restating it may reach the model when masked.
    assert ("rivate health information" in model.calls[0].system) is offered
    prose_json = approved(model.calls[0])["owner_approved_prose"]
    assert (prose_json["original"] is not None) is offered
    assert (result.verdict, result.stages[-1].reason_code) == (verdict, reason)
    if offered:
        assert result.stages[-1].matched_deny_clause_ids == ["deny-health"]
        [exclusion] = prose_json["exclusions"]
        assert (exclusion["sources"], len(exclusion["tables"])) == (["ai-source-1"], 3)
        tables = {unit["unit_id"]: unit["table"] for unit in json.loads(model.calls[0].candidate_data)["untrusted_candidate_data"]}
        assert sorted(tables[unit] for unit in exclusion["structural_scope_unit_ids"]) == ["ai_chat_messages", "signal_objects"]


@pytest.mark.asyncio
async def test_output_stage_exclusion_is_scoped_to_the_exact_output(timed, projection_service):
    raw = policy(timed)
    raw["rules"].append(rule("deny-health", "deny", "health"))
    record_output(timed, projection_service)
    model = Fake()
    await bridge(timed, projection_service, raw=raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    [evidence_exclusion] = approved(model.calls[0])["owner_approved_prose"]["exclusions"]
    [output_exclusion] = approved(model.calls[1])["owner_approved_prose"]["exclusions"]
    assert evidence_exclusion["structural_scope_unit_ids"] == ["u1", "u2"]
    assert output_exclusion["structural_scope_unit_ids"] == ["output"]


@pytest.mark.asyncio
async def test_exclusion_reach_is_presented_as_scope_never_as_a_match(timed, projection_service):
    """Nothing in this positive is about health, yet the deny clause's sources,
    tables and window reach every unit. Listed as `unit_ids` under a prompt saying
    the exclusion "applies" to them, that reach read as a finding against the one
    synthetic positive. It must be named, and described, as scope.
    """
    raw = policy(timed)
    raw["rules"].append(rule("deny-health", "deny", "health"))
    record_output(timed, projection_service)
    model = Fake()
    result = await bridge(timed, projection_service, raw=raw, transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert result.verdict == "permit" and len(model.calls) == 2
    for call in model.calls:
        [exclusion] = approved(call)["owner_approved_prose"]["exclusions"]
        assert set(exclusion) == {"clause_id", "text", "sources", "tables", "structural_scope_unit_ids"}
        assert exclusion["structural_scope_unit_ids"]
        assert "unit_ids" not in call.system.replace("structural_scope_unit_ids", "")
        assert "That list is scope, not a match" in call.system and "applies only to the candidate units" not in call.system


def test_prompt_revision_is_pinned_to_its_template_and_version():
    """Any edit to the template must come with a deliberate version bump and a new pin."""
    assert FACT_PROMPT_REVISION == digest({"template": FACT_SYSTEM_PROMPT, "version": "fact-bridge-prompt/v3"})
    assert FACT_PROMPT_REVISION == "f57ae6f3ee18ea6445f5fbe53e6a0dca7b1dffbc7b225a9cd232c38cd99767d2"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["evidence_use", "output_release"])
@pytest.mark.parametrize("change", ["protect", "revoke_evidence", "edit_row", "revoke_output", "expire_clock"])
async def test_change_during_the_model_call_is_never_retained_or_cached(timed, projection_service, change, stage):
    recorded = record_output(timed, projection_service)
    clock = [AS_OF]
    cached_before_call = []

    def mutate(request):
        if request.stage != stage:
            return
        cached_before_call.append(len(cache._entries))
        if change == "protect":
            with sqlite3.connect(timed[0].path) as conn:
                RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="message-1")
        elif change == "revoke_evidence":
            with owner():
                timed[1].revoke_review("review-1")
        elif change == "edit_row":
            edit(timed, "UPDATE conversation_messages SET content='Changed claim.'")
        elif change == "revoke_output":
            with owner():
                projection_service.revoke(RevokeProjectionReview(fact_id=timed[2], review_id=recorded.review_id,
                    expected_review_revision=recorded.review_revision), now=AS_OF)
        else:
            clock[0] = AS_OF + 121
    cache, model = ShadowDecisionCache(), Fake(callback=mutate)
    result = await bridge(timed, projection_service, transport=model, clock=lambda: clock[0], cache=cache).run(
        timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert result.observation_state == "not_retained" and result.verdict == "indeterminate"
    assert result.stages[-1].reason_code == "requalification_failed"
    # A change during stage 1 is caught before output_release sends the
    # reviewed scalar; one during stage 2 is caught before the permit is kept.
    expected = ["evidence_use"] if stage == "evidence_use" else ["evidence_use", "output_release"]
    assert [call.stage for call in model.calls] == expected and result.model_calls == len(expected)
    assert [item.stage for item in result.stages] == expected + [stage]
    # Stage 1's permit was cached before the stage-2 call and is evicted with it.
    assert cached_before_call == [len(expected) - 1] and cache._entries == {}


class WrongRevision(Fake):
    async def complete(self, request):
        response = await super().complete(request)
        return ModelResponse(**{**response.model_dump(), "model_revision": "8" * 64})


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_post_call_identity_mismatch_counts_the_call_and_requalifies(timed, projection_service, revoked):
    record_output(timed, projection_service)

    def mutate(request):
        if revoked:
            with owner():
                timed[1].revoke_review("review-1")
    model, cache = WrongRevision(callback=mutate), ShadowDecisionCache()
    result = await bridge(timed, projection_service, transport=model, cache=cache).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert len(model.calls) == 1 and result.model_calls == 1 and result.verdict == "indeterminate"
    assert result.stages[0].reason_code == "model_identity" and cache._entries == {}
    if revoked:
        assert (result.observation_state, result.stages[-1].reason_code) == ("not_retained", "requalification_failed")
    else:
        assert result.observation_state == "requalified" and len(result.stages) == 1


@pytest.mark.asyncio
async def test_cache_reuses_only_the_identical_capture_and_never_operational_failures(timed, projection_service):
    record_output(timed, projection_service)
    model, cache = Fake(), ShadowDecisionCache()
    first = await bridge(timed, projection_service, transport=model, cache=cache).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    second = await bridge(timed, projection_service, transport=model, cache=cache).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert first.verdict == second.verdict == "permit" and len(model.calls) == 2
    assert second.model_calls == 0 and second.observation_state == "captured_under_gates"
    later = await bridge(timed, projection_service, transport=model, cache=cache, clock=lambda: AS_OF + 1).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert later.model_calls == 2 and len(model.calls) == 4
    slow = Fake(delay=1.0)
    for _ in range(2):
        result = await bridge(timed, projection_service, transport=slow, cache=cache, clock=lambda: AS_OF + 5).run(timed[2], request_as_of=AS_OF + 5, arm="semantic_v1")
        assert result.stages[-1].reason_code == "model_timeout" and result.verdict == "indeterminate"
    assert len(slow.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case,verdict,code", [("owner_only", "deny", "owner_only"), ("no_output_review", "indeterminate", "output_review_required"),
    ("entity_protection", "indeterminate", "entity_protection_lineage_unavailable"), ("surface_budget", "indeterminate", "surface_budget")])
async def test_withheld_evidence_never_reaches_either_arm_or_a_model(timed, projection_service, case, verdict, code):
    if case == "surface_budget":
        edit(timed, "UPDATE conversation_messages SET content=?", ("x" * 16001,))
    if case == "no_output_review":
        attest(timed)
    else:
        record_output(timed, projection_service)
    if case == "owner_only":
        payload(timed, disclosure="owner_only")
    elif case == "entity_protection":
        edit(timed, "INSERT INTO entity_blackholes(blackhole_id,normalized_name,entity_id) VALUES('blackhole-1','synthetic entity','entity-2')")
    model = Fake()
    results = await both(timed, projection_service, transport=model)
    assert model.calls == []
    for arm, result in results.items():
        [decision] = result.stages
        assert (decision.verdict, decision.reason_code, decision.withheld_code) == (verdict, "evidence_withheld", code), arm
        assert result.model_calls == 0


@pytest.mark.parametrize("damage", ["foreign_inclusion", "missing_exclusion", "example_amends_exclusion", "edited_prose", "budget"])
def test_capsule_is_closed_and_mirrors_the_policy_rules(timed, damage):
    raw = policy(timed)
    raw["rules"].append(rule("deny-health", "deny", "health"))
    def broken(body):
        if damage == "foreign_inclusion":
            body["prose"]["inclusions"].append({"clause_id": "not-a-rule", "text": "Anything."})
        elif damage == "missing_exclusion":
            body["prose"]["exclusions"] = []
        elif damage == "example_amends_exclusion":
            body["prose"]["examples"].append({"example_id": "ex-2", "clause_id": "deny-health", "verdict": "permit", "role": "illustration", "text": "x"})
        elif damage == "edited_prose":
            body["prose"]["original"] = "Share everything."
        else:
            body["processor"]["max_prompt_bytes"] = 100
    with pytest.raises(PolicyError, match="schema_invalid"):
        capsule(raw, edit_after=broken)


@pytest.mark.asyncio
async def test_binding_and_prompt_pins_are_enforced_before_any_call(timed, projection_service):
    record_output(timed, projection_service)
    raw = policy(timed)
    with pytest.raises(PolicyError, match="fact_policy_binding"):
        FactShadowBridge(capsule(raw), projections=projection_service, binding=Binding.parse({**raw["binding"], "actor_id": "other"}), clock=lambda: AS_OF)
    model = Fake()
    result = await bridge(timed, projection_service, transport=model, prompt_revision="9" * 64).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert result.stages[-1].reason_code == "model_identity" and model.calls == [] and result.model_calls == 0
    absent = await bridge(timed, projection_service).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert absent.stages[-1].reason_code == "unconfigured_model" and absent.verdict == "indeterminate"


EXPERIMENTS = "topos.permissions_v2.experiments"


def _imported_modules(path, package):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[:len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([base] if base else []))
            yield base
            yield from (base + "." + alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(EXPERIMENTS):
            yield node.value  # importlib.import_module("topos.permissions_v2.experiments...")


def test_no_serving_module_imports_the_experiment_package():
    root = Path(__file__).resolve().parents[2]
    experiments = root / "topos" / "permissions_v2" / "experiments"
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


def test_import_boundary_scan_resolves_relative_imports(tmp_path):
    for source, package in (("from .experiments import fact_bridge", "topos.permissions_v2"),
                            ("from ..permissions_v2 import experiments", "topos.api"),
                            ("import topos.permissions_v2.experiments.fact_bridge", "topos.api")):
        path = tmp_path / "probe.py"
        path.write_text(source)
        assert any(name.startswith(EXPERIMENTS) for name in _imported_modules(path, package)), source
