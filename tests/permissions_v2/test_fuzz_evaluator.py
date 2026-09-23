"""Fuzz lane, part 1: the three-valued evaluator and the raw-message decision (design §3.1-3.2).

Each invariant of CONFIDENCE_PROGRAM_PLAN.md C5 is stated exactly as the code makes it true:

K1  Kleene. `evaluate_predicate` agrees with an independent reference evaluator on every
    predicate and attribute map: NOT, ALL_OF and ANY_OF follow the strong Kleene tables,
    and an unknown atom is Unknown under every connective.
K2  Unknown withholds, evaluator level. Making any attribute unknown never turns a definite
    result into the opposite definite result: the result stays, or becomes Unknown.
K3  Unknown withholds, decision level. Over the review vocabulary the raw-message decision
    can never see an Unknown atom at all -- `_attributes` always yields four string lists --
    so on p2a the unknown gate IS the floors (`_eligible` refuses `sensitivity: unknown`,
    empty domains and every unknown floor field). K3 pins that; the fact path, where
    Unknown arises from time, is in test_fuzz_fact_decisions.
N1  Narrowing is monotone, policy side. Adding a deny rule, dropping a permit rule,
    strengthening a permit predicate, weakening a deny predicate, shrinking a permit rule's
    sources, tables or ceiling, and widening a deny rule's sources or tables never move a
    verdict toward permit (deny < indeterminate < permit).
N2  Narrowing is monotone, label side, in its exact form. Adding to one item a domain
    value, or raising its sensitivity to a value, that the policy names only on the deny
    side positively or the permit side negatively never moves the verdict toward permit.
    The design's sentence "adding a label never widens" is true of exactly those values --
    every private value of a work-only preset -- and N2a keeps the witness that a value a
    permit atom names positively can widen (a spurious `work` label on a hobby message),
    which is why D20 bounds sensitive misses and not spurious sensitive labels.
S1  A decision is a pure function of (policy, evidence): deterministic, carrying the
    policy's digest, and a permit names exactly one allow clause and the capability's view,
    a deny its deny clauses and no view, an indeterminate the missing classification.
S2  Evidence qualified under another capability's subject rule, or with an incomplete or
    mismatched classification set, is refused with the same code, never evaluated.
"""
from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")
from hypothesis import assume, given, settings, strategies as st  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from topos.permissions_v2.canonical import PolicyError, digest  # noqa: E402
from topos.permissions_v2.contract import Predicate, evaluate_predicate  # noqa: E402
from topos.permissions_v2.evidence import QualifiedEvidence  # noqa: E402
from topos.permissions_v2.identity import ATTESTED_CONTRACT, LEGACY_CONTRACT  # noqa: E402
from topos.permissions_v2.registry import parse_policy  # noqa: E402
from topos.permissions_v2.release import _attributes, source_message_decision  # noqa: E402

pytestmark = [pytest.mark.fuzz]
PREDICATE = TypeAdapter(Predicate)
PURE = settings(max_examples=fz.examples("pure"))


def reference(predicate: dict, attributes: dict):
    """Strong Kleene, written from the design's table and not from the code."""
    kind = predicate["kind"]
    if kind == "atom":
        value = attributes.get(predicate["attribute"])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return None
        return bool(set(value) & set(predicate["values"]))
    if kind == "not":
        inner = reference(predicate["term"], attributes)
        return None if inner is None else not inner
    results = [reference(term, attributes) for term in predicate["terms"]]
    if kind == "all_of":
        return False if False in results else None if None in results else True
    return True if True in results else None if None in results else False


def decide(raw, evidence):
    return source_message_decision(parse_policy(raw), evidence)


# --- K1, K2, K3 --------------------------------------------------------------------------------------

@PURE
@given(fz.predicates(), fz.attribute_maps())
def test_K1_evaluator_matches_the_reference_kleene_tables(predicate, attributes):
    parsed = PREDICATE.validate_python(predicate)
    assert evaluate_predicate(parsed, attributes) is reference(predicate, attributes)
    assert evaluate_predicate(parsed, attributes) is evaluate_predicate(parsed, attributes)


@PURE
@given(fz.predicates(), fz.attribute_maps(known_only=True), st.data())
def test_K2_making_an_attribute_unknown_never_flips_a_definite_result(predicate, attributes, data):
    parsed = PREDICATE.validate_python(predicate)
    before = evaluate_predicate(parsed, attributes)
    weakened = fz.weaken(data.draw, attributes)
    after = evaluate_predicate(parsed, weakened)
    assert after is None or after is before


@PURE
@given(fz.predicates(), fz.source_evidence())
def test_K3_the_review_vocabulary_never_reaches_the_evaluator_as_unknown(predicate, evidence):
    parsed = PREDICATE.validate_python(predicate)
    for item in evidence.classifications:
        assert evaluate_predicate(parsed, _attributes(item)) is not None


# --- S1, S2 --------------------------------------------------------------------------------------

@PURE
@given(st.data())
def test_S1_a_decision_is_a_pure_function_with_a_closed_shape(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    policy = parse_policy(raw)
    first, second = decide(raw, evidence), decide(raw, evidence)
    assert first == second
    assert first.policy_hash == digest(policy.model_dump())
    assert first.stage == "output_release" and first.evaluator_version == fz.EVALUATORS[capability]
    if first.verdict == "permit":
        assert len(first.matched_allow_clause_ids) == 1 and first.reason_code == "rule_permit"
        assert first.required_projection_id == fz.VIEWS[capability] and first.missing_context_codes == []
        assert first.matched_deny_clause_ids == []
    elif first.verdict == "deny":
        assert first.matched_allow_clause_ids == [] and first.required_projection_id is None
        assert first.reason_code == "rule_deny" and first.missing_context_codes == []
    else:
        assert first.reason_code == "unknown_context" and first.missing_context_codes == ["classification"]
        assert first.matched_allow_clause_ids == [] and first.required_projection_id is None


@PURE
@given(st.data())
def test_S2_evidence_under_another_subject_rule_is_refused_never_evaluated(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    expected = {fz.CAPABILITY: LEGACY_CONTRACT}.get(capability, ATTESTED_CONTRACT)
    other = ATTESTED_CONTRACT if expected == LEGACY_CONTRACT else LEGACY_CONTRACT
    evidence = data.draw(fz.source_evidence(capability, contract=other))
    with pytest.raises(PolicyError) as refused:
        decide(raw, evidence)
    assert refused.value.code == "subject_contract_mismatch"


@PURE
@given(st.data())
def test_S2_an_incomplete_or_mismatched_classification_set_is_refused(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    dumped = evidence.model_dump()
    change = data.draw(st.sampled_from(["drop", "foreign", "foreign_revision"]))
    items = dumped["classifications"]
    index = data.draw(st.integers(0, len(items) - 1))
    if change == "drop":
        items.pop(index)
    elif change == "foreign":
        stray = dict(items[index])
        stray["evidence"] = {**stray["evidence"], "identity": {**stray["evidence"]["identity"], "record_id": "rec-stray"}}
        items[index] = stray
    else:
        # The same identity at another revision: the floor refuses it as stale; the decision keys
        # labels by identity alone, so this one is the floor's to refuse (S2 pins that below).
        stray = dict(items[index])
        stray["evidence"] = {**stray["evidence"], "revision": "f" * 64}
        items[index] = stray
        with pytest.raises(PolicyError):
            fz.reviewed_by_floor(QualifiedEvidence(**dumped))
        return
    with pytest.raises(PolicyError) as refused:
        decide(raw, QualifiedEvidence(**dumped))
    assert refused.value.code == "classification_incomplete"


def test_S2a_finding_a_duplicated_classification_is_the_floors_to_refuse_not_the_decisions():
    """Recorded by the lane on 22 Sep 2026 (phase-0 report, finding C5-1).

    `source_message_decision` keys the review labels by evidence identity, so a review list
    that names one item twice collapses to one entry and passes the `len(labels) ==
    len(closure)` check. The decision is only ever handed a review the floor
    (`EvidenceResolver._eligible`) has already checked for exactly that duplication, so the
    gap is unreachable through any door; it is pinned here so a future caller of the pure
    function cannot rely on it, and reported to the design session as a one-line
    defence-in-depth check rather than changed on this branch (enforcement core: plan first).
    """
    evidence = fz.fixed_evidence(fz.CAPABILITY_ATTESTED)
    dumped = evidence.model_dump()
    dumped["classifications"].append(dict(dumped["classifications"][-1]))
    duplicated = QualifiedEvidence(**dumped)
    raw = fz.source_policy(fz.CAPABILITY_ATTESTED, [])
    assert decide(raw, duplicated).verdict == decide(raw, evidence).verdict  # the decision tolerates it ...
    with pytest.raises(PolicyError) as refused:                                # ... the floor does not
        fz.reviewed_by_floor(duplicated)
    assert refused.value.code == "classification_incomplete"


# --- N1: policy-side narrowing -----------------------------------------------------------------------

@PURE
@given(st.data())
def test_N1_every_policy_narrowing_is_monotone(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    name = data.draw(st.sampled_from(fz.NARROWINGS))
    narrowed = fz.narrow(raw, name, data.draw, capability)
    assume(narrowed is not None)
    before, after = decide(raw, evidence), decide(narrowed, evidence)
    assert fz.at_most(after.verdict, before.verdict), (name, before.verdict, after.verdict)


@PURE
@given(st.data())
def test_N1_a_chain_of_narrowings_never_climbs(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    verdicts = [decide(raw, evidence).verdict]
    for _ in range(data.draw(st.integers(1, 4))):
        narrowed = fz.narrow(raw, data.draw(st.sampled_from(fz.NARROWINGS)), data.draw, capability)
        if narrowed is None:
            continue
        raw = narrowed
        verdicts.append(decide(raw, evidence).verdict)
    for earlier, later in zip(verdicts, verdicts[1:]):
        assert fz.at_most(later, earlier), verdicts


# --- N2: label-side narrowing ---------------------------------------------------------------------

@PURE
@given(st.data())
def test_N2_adding_a_deny_side_domain_label_never_widens(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    index = data.draw(st.integers(0, len(evidence.classifications) - 1))
    value = data.draw(st.sampled_from(fz.VOCABULARY))
    item = evidence.classifications[index]
    assume(value not in item.domains)
    assume(fz.safe_to_add(raw, "domain", value))
    before = decide(raw, evidence)
    after = decide(raw, fz.with_classification(evidence, index, domains=list(item.domains) + [value]))
    assert fz.at_most(after.verdict, before.verdict), (value, before.verdict, after.verdict)


@PURE
@given(st.data())
def test_N2_raising_a_deny_side_sensitivity_never_widens(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    index = data.draw(st.integers(0, len(evidence.classifications) - 1))
    item = evidence.classifications[index]
    higher = [s for s in fz.SENSITIVITIES if fz.SENSITIVITIES.index(s) > fz.SENSITIVITIES.index(item.sensitivity)]
    assume(higher)
    value = data.draw(st.sampled_from(higher))
    assume(fz.safe_to_add(raw, "sensitivity", value) and fz.safe_to_remove(raw, "sensitivity", item.sensitivity))
    before = decide(raw, evidence)
    after = decide(raw, fz.with_classification(evidence, index, sensitivity=value))
    assert fz.at_most(after.verdict, before.verdict), (item.sensitivity, value, before.verdict, after.verdict)


def test_N2a_witness_a_permit_side_label_can_widen_which_is_why_misses_are_the_bound():
    """Cell C's permit names `work` positively: a hobby message falsely labelled work is released.

    Not a defect of the evaluator but a fact about labels: a spurious sensitive label only
    narrows (N2), a spurious permit-side label widens, and a MISSED sensitive label is the
    failure D20 bounds. Kept as a witness so the statement in the report stays exact.
    """
    work_none = {"kind": "all_of", "terms": [fz._atom("domain", ["work"]), fz._atom("sensitivity", ["none"])]}
    rules = [fz.source_rule("permit-work", "permit", sources=dict(fz.ALL_SOURCES), predicate=work_none,
                            release_predicate=work_none, ceiling="raw",
                            forms=[fz.form(fz.CAPABILITY_ATTESTED, fz.LEAF_TABLES)]),
             fz.source_rule("deny-private", "deny", sources=dict(fz.ALL_SOURCES),
                            predicate=fz._atom("domain", ["health", "finance", "home", "relationships", "family"]),
                            release_predicate=fz._atom("sensitivity", ["personal", "special"]), ceiling="raw",
                            forms=[fz.form(fz.CAPABILITY_ATTESTED, fz.LEAF_TABLES)])]
    raw = fz.source_policy(fz.CAPABILITY_ATTESTED, rules)
    evidence = fz.fixed_evidence(fz.CAPABILITY_ATTESTED)
    hobby = fz.with_classification(evidence, 0, domains=["hobbies"], sensitivity="none")
    hobby = fz.with_classification(hobby, 1, domains=["hobbies"], sensitivity="none")
    assert decide(raw, hobby).verdict == "deny"
    assert not fz.safe_to_add(raw, "domain", "work")
    widened = fz.with_classification(fz.with_classification(hobby, 0, domains=["hobbies", "work"]), 1,
                                     domains=["hobbies", "work"])
    assert decide(raw, widened).verdict == "permit"
    # And the deny-side value narrows, as N2 says.
    assert fz.safe_to_add(raw, "domain", "health")
    assert decide(raw, fz.with_classification(widened, 1, domains=["hobbies", "work", "health"])).verdict == "deny"


# --- S3: a permit rule that does not cover the closure never permits ---------------------------------

def _leaf_sources(evidence):
    return {leaf.identity.source_id for leaf in evidence.snapshot.leaves}


def _leaf_tables(evidence):
    return {leaf.identity.table for leaf in evidence.snapshot.leaves}


def _rule_sources(rule, raw):
    selection = rule["evidence_use"]["sources"]
    return set(selection["values"]) if selection["kind"] == "only" else set(raw["source_universe"]["source_ids"])


def _rule_tables(rule):
    return {table for form_ in rule["release"]["forms"] for table in form_["tables"]}


def _covering_permits(raw, evidence):
    return [rule for rule in raw["rules"] if rule["effect"] == "permit" and rule["release"]["ceiling"] == "raw"
            and "owner-engine-local" in rule["evidence_use"]["processors"]["values"]
            and _leaf_sources(evidence) <= _rule_sources(rule, raw) and _leaf_tables(evidence) <= _rule_tables(rule)]


@PURE
@given(st.data())
def test_S3_only_a_raw_permit_rule_covering_every_leaf_source_and_table_can_permit(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    decision = decide(raw, evidence)
    covering = _covering_permits(raw, evidence)
    if not covering:
        assert decision.verdict != "permit", (decision.verdict, [r["rule_id"] for r in raw["rules"]])
    elif decision.verdict == "permit":
        assert decision.matched_allow_clause_ids[0] in {rule["rule_id"] for rule in covering}


@pytest.mark.parametrize("capability", fz.CAPABILITIES)
@pytest.mark.parametrize("gap", ["sources", "tables", "none"])
def test_S3b_a_permit_rule_that_misses_a_leaf_source_or_table_never_permits(capability, gap):
    """The directed case behind S3: a raw, local, trivially-true permit that names every leaf permits, and the same
    rule with one leaf's source or table outside its selection does not (the battery's `permit_ignores_uncovered_*`
    survived the targeted lane, which drew such a rule too rarely)."""
    evidence = fz.fixed_evidence(capability, leaves=1)
    leaf = evidence.snapshot.leaves[0].identity
    sources = {"kind": "only", "values": [source for source in fz.UNIVERSE if source != leaf.source_id] if gap == "sources"
               else [leaf.source_id]}
    tables = [table for table in fz.LEAF_TABLES if table != leaf.table] if gap == "tables" else [leaf.table]
    rule = fz.source_rule("permit-directed", "permit", sources=sources, predicate=fz.TRUE, release_predicate=fz.TRUE,
                          ceiling="raw", forms=[fz.form(capability, tables)])
    decision = decide(fz.source_policy(capability, [rule]), evidence)
    if gap == "none":
        assert decision.verdict == "permit", (decision.verdict, decision.reason_code)
    else:
        assert decision.verdict != "permit", (gap, decision.verdict, decision.reason_code)


@PURE
@given(st.data())
def test_S3_a_deny_rule_only_fires_on_a_leaf_it_selects(data):
    capability = data.draw(st.sampled_from(fz.CAPABILITIES))
    raw = data.draw(fz.source_policies(capability))
    evidence = data.draw(fz.source_evidence(capability))
    decision = decide(raw, evidence)
    for rule_id in decision.matched_deny_clause_ids:
        rule = next(rule for rule in raw["rules"] if rule["rule_id"] == rule_id)
        assert rule["effect"] == "deny"
        assert any(leaf.identity.source_id in _rule_sources(rule, raw) and leaf.identity.table in _rule_tables(rule)
                   for leaf in evidence.snapshot.leaves), rule_id
