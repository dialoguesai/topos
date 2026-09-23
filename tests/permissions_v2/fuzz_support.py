"""Shared strategies and helpers of the permissions v2 fuzz lane (confidence program, C5).

The lane states the design's structural invariants (SCALABLE_GRANTS_DESIGN.md §3 and
§6.2; CONFIDENCE_PROGRAM_PLAN.md C5) as Hypothesis properties over generated policies,
reviewed evidence, JSON values, signed envelopes and whole SQLite corpora. Every value
is invented: nothing here opens the owner's home, a network socket, a model or a real
database, and no test writes outside pytest's tmp_path.

Two profiles, registered in conftest.py and chosen with TOPOS_FUZZ_PROFILE:

  lane   deterministic seeds, modest example counts: the permanent lane every run pays
  deep   random seeds, larger counts: the confidence run recorded in the phase-0 report

`examples(pure, door)` lets a test scale its count by profile; door tests build a corpus
per example and are kept small. No example database is written (`database=None`), so a
run leaves nothing behind in the tree.
"""
from __future__ import annotations

import os
from copy import deepcopy

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import strategies as st  # noqa: E402

from topos.permissions_v2.contract import (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE, VIEW,  # noqa: E402
    VIEW_OPAQUE)
from topos.permissions_v2.evidence import (EvidenceBinding, EvidenceIdentity, EvidenceRevision,  # noqa: E402
    EvidenceSnapshot, QualifiedEvidence, ReviewedClassification, _key)
from topos.permissions_v2.identity import SUBJECT_CONTRACT_BY_CAPABILITY  # noqa: E402

PROFILE = os.environ.get("TOPOS_FUZZ_PROFILE", "lane")
EXAMPLES = {"lane": {"pure": 80, "door": 6}, "deep": {"pure": 500, "door": 24}}


def examples(kind: str) -> int:
    return EXAMPLES.get(PROFILE, EXAMPLES["lane"])[kind]


# --- the vocabulary the campaign fixed (CAMPAIGN_DATASET.md) plus the evaluator's other attributes -----
VOCABULARY = ("work", "plans", "hobbies", "health", "family", "finance", "relationships", "home")
SENSITIVITIES = ("none", "personal", "special")
ATTRIBUTE_VALUES = {"domain": VOCABULARY, "sensitivity": SENSITIVITIES,
                    "actor_role": ("authored", "received"), "subject": ("owner", "other")}
LEAF_TABLES = ("conversation_messages", "ai_chat_messages")
UNIVERSE = ("source-A", "source-B", "source-C")
CAPABILITIES = (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE)
EVALUATORS = {CAPABILITY: "hard-rules/p2a-v1", CAPABILITY_ATTESTED: "hard-rules/p2a-v2",
              CAPABILITY_OPAQUE: "hard-rules/p2a-v3"}
VIEWS = {CAPABILITY: VIEW, CAPABILITY_ATTESTED: VIEW, CAPABILITY_OPAQUE: VIEW_OPAQUE}
VERDICT_ORDER = {"deny": 0, "indeterminate": 1, "permit": 2}
SUBJECT_BINDING = {"contract": "owner_attested_v1", "statement_version": "owner-identity-attestation/v1",
                   "subjects": "owner_attested_entities", "unattested": "withhold",
                   "moved_since_attestation": "withhold", "rekeyed_facts": "withhold",
                   "literal_self_when_shadowed": "withhold"}
BINDING = EvidenceBinding(environment_id="permissions-beta-fuzz", node_id="node-1", resource_id="resource-1",
                          owner_id="owner-1")
HARD_CONSTRAINTS = {"owner_only": "deny", "unknown_classification": "withhold", "unknown_lineage": "withhold",
                    "cross_rule_derivation": "deny", "capability_growth": "require_consent"}
ALL_SOURCES = {"kind": "all", "universe_id": "universe-1", "universe_revision": 1, "growth": "require_consent"}

hex64 = st.binary(min_size=32, max_size=32).map(bytes.hex)
identifiers = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,11}", fullmatch=True)


def at_most(after: str, before: str) -> bool:
    """Verdict order deny < indeterminate < permit: `after` did not move toward permit."""
    return VERDICT_ORDER[after] <= VERDICT_ORDER[before]


# --- predicates and attribute maps ------------------------------------------------------------------

def _atom(attribute, values):
    return {"kind": "atom", "attribute": attribute, "operator": "intersects", "values": list(values)}


@st.composite
def atoms(draw):
    attribute = draw(st.sampled_from(sorted(ATTRIBUTE_VALUES)))
    values = draw(st.lists(st.sampled_from(ATTRIBUTE_VALUES[attribute]), min_size=0, max_size=3, unique=True))
    return _atom(attribute, values)


TRUE = {"kind": "all_of", "terms": []}     # the empty conjunction: True under strong Kleene, in the grammar


def predicates(max_leaves: int = 6):
    return st.recursive(
        atoms(),
        lambda inner: st.one_of(
            st.lists(inner, min_size=0, max_size=3).map(lambda terms: {"kind": "all_of", "terms": terms}),
            st.lists(inner, min_size=0, max_size=3).map(lambda terms: {"kind": "any_of", "terms": terms}),
            inner.map(lambda term: {"kind": "not", "term": term})),
        max_leaves=max_leaves)


def attribute_lists(attribute):
    return st.lists(st.sampled_from(ATTRIBUTE_VALUES[attribute]), min_size=0, max_size=3, unique=True)


def unknown_values():
    """Every shape `evaluate_predicate` treats as Unknown: absent, null, not a list, a list with a non-string."""
    return st.sampled_from([None, "malformed", 7, {"domain": ["work"]}, [1], ["work", None], ["work", 3]])


@st.composite
def attribute_maps(draw, known_only: bool = False):
    result = {}
    for attribute in sorted(ATTRIBUTE_VALUES):
        choice = draw(st.integers(0, 3)) if not known_only else 0
        if choice == 0:
            result[attribute] = draw(attribute_lists(attribute))
        elif choice == 1:
            continue  # absent
        else:
            result[attribute] = draw(unknown_values())
    return result


def weaken(draw, attributes: dict) -> dict:
    """The same map with a drawn subset of its known attributes made Unknown (absent or malformed)."""
    weakened = dict(attributes)
    for attribute in sorted(attributes):
        if isinstance(attributes[attribute], list) and draw(st.booleans()):
            if draw(st.booleans()):
                weakened.pop(attribute)
            else:
                weakened[attribute] = draw(unknown_values())
    return weakened


# --- raw-message (p2a) policies ---------------------------------------------------------------------

def form(capability, tables):
    return {"family": "canonical_record", "operation": "read", "view_id": VIEWS[capability], "tables": list(tables)}


def source_rule(rule_id, effect, *, sources, predicate, release_predicate, ceiling, forms,
                processors=("owner-engine-local",)):
    return {"rule_id": rule_id, "effect": effect,
            "evidence_use": {"sources": sources, "predicate": predicate, "purpose": "fuzz-reading",
                             "processors": {"kind": "only", "values": list(processors)},
                             "new_records": "include_if_predicate"},
            "release": {"predicate": release_predicate, "ceiling": ceiling, "forms": forms}}


def source_policy(capability, rules, *, universe=UNIVERSE, budget=None, grant="grant-1"):
    versions = {"vocabulary": "owner-review-vocabulary/v1", "capability": capability}
    if capability != CAPABILITY:
        versions["subject_binding"] = dict(SUBJECT_BINDING)
    raw = {"version": "topos-policy/v2", "policy_version_id": "policy-fuzz",
           "binding": {**BINDING.model_dump(), "actor_id": "actor-1", "client_id": "client-1", "grant_id": grant,
                       "assignment_id": "assignment-1"},
           "versions": versions, "validity": {"starts_at": 1000, "expires_at": 5000},
           "source_universe": {"universe_id": "universe-1", "revision": 1, "source_ids": list(universe)},
           "hard_constraints": dict(HARD_CONSTRAINTS), "rules": list(rules),
           "evaluator": {"kind": "hard_rules", "version": EVALUATORS[capability]}, "natural_language": None}
    if budget is not None:
        raw["read_budget_per_day"] = budget
    return raw


def sources():
    return st.one_of(st.lists(st.sampled_from(UNIVERSE), min_size=0, max_size=3, unique=True)
                     .map(lambda values: {"kind": "only", "values": values}),
                     st.just(dict(ALL_SOURCES)))


def tables():
    return st.lists(st.sampled_from(LEAF_TABLES), min_size=0, max_size=2, unique=True)


@st.composite
def source_rules(draw, capability, max_rules: int = 4):
    count = draw(st.integers(0, max_rules))
    rules = []
    for index in range(count):
        forms = [form(capability, draw(tables())) for _ in range(draw(st.integers(0, 2)))]
        # One draw in two takes the trivially-true predicate (an empty conjunction), so a rule that covers the
        # leaves fires often enough for S3 to see it: the battery's `permit_ignores_uncovered_*` survived the
        # targeted lane before this, because a random predicate over random labels seldom evaluates True.
        rules.append(source_rule(f"rule-{index}", draw(st.sampled_from(["permit", "deny"])),
                                 sources=draw(sources()), predicate=draw(st.one_of(predicates(), st.just(TRUE))),
                                 release_predicate=draw(st.one_of(predicates(), st.just(TRUE))),
                                 ceiling=draw(st.sampled_from(["raw", "summary", "inference"])), forms=forms,
                                 processors=draw(st.sampled_from([("owner-engine-local",), ()]))))
    return rules


@st.composite
def source_policies(draw, capability=None):
    capability = capability or draw(st.sampled_from(CAPABILITIES))
    return source_policy(capability, draw(source_rules(capability)))


# --- reviewed evidence for the pure raw-message decision -------------------------------------------------

def identity(table, record_id, source_id=None):
    dataset = table == "conversation_messages"
    return EvidenceIdentity(binding=BINDING, table=table, record_id=record_id, source_id=source_id,
                            dataset_kind="row_dataset" if dataset else "node_resource",
                            dataset_id="dataset-1" if dataset else None)


def classification(revision, domains, sensitivity):
    return ReviewedClassification(evidence=revision, domains=list(domains), sensitivity=sensitivity,
                                  subject_entity_ids=["self"], authorship="owner_authored",
                                  speech="direct_self_statement", independent_copies="none_known")


@st.composite
def source_evidence(draw, capability=None, contract=None, max_leaves: int = 4):
    """A snapshot of one fact over 1..max_leaves owner messages, every item reviewed in the vocabulary."""
    capability = capability or draw(st.sampled_from(CAPABILITIES))
    contract = contract or SUBJECT_CONTRACT_BY_CAPABILITY[capability]
    leaves = []
    for index in range(draw(st.integers(1, max_leaves))):
        leaves.append(identity(draw(st.sampled_from(LEAF_TABLES)), f"rec-{index}", draw(st.sampled_from(UNIVERSE))))
    root_revision = draw(hex64)
    snapshot = EvidenceSnapshot(binding=BINDING, canonical_file_revision=draw(hex64), fact_id="fact-1",
                                candidate_revision=root_revision, lineage_revision=draw(hex64),
                                protection_revision=draw(hex64),
                                artifacts=[EvidenceRevision(identity=identity("signal_objects", "fact-1"),
                                                            revision=root_revision)],
                                leaves=[EvidenceRevision(identity=leaf, revision=draw(hex64)) for leaf in leaves])
    classifications = [classification(item, draw(attribute_lists("domain")), draw(st.sampled_from(SENSITIVITIES)))
                       for item in snapshot.artifacts + snapshot.leaves]
    return QualifiedEvidence(family="owner_stated_fact/v1", snapshot=snapshot, review_id="review-1",
                             review_revision=draw(hex64), classifications=classifications,
                             subject_contract=contract, execution_enabled=False)


def with_classification(evidence: QualifiedEvidence, index: int, *, domains=None, sensitivity=None):
    """The same evidence with one item's review changed; the snapshot is untouched."""
    items = list(evidence.classifications)
    item = items[index]
    items[index] = ReviewedClassification(evidence=item.evidence,
                                          domains=list(item.domains if domains is None else domains),
                                          sensitivity=item.sensitivity if sensitivity is None else sensitivity,
                                          subject_entity_ids=item.subject_entity_ids, authorship=item.authorship,
                                          speech=item.speech, independent_copies=item.independent_copies)
    return QualifiedEvidence(**{**evidence.model_dump(), "classifications": [c.model_dump() for c in items]})


# --- polarity: where a policy names a value, and on which side -------------------------------------------

def _positions(predicate, polarity, out, side):
    kind = predicate["kind"]
    if kind == "atom":
        for value in predicate["values"]:
            out.setdefault((side, predicate["attribute"], value), set()).add(polarity)
    elif kind == "not":
        _positions(predicate["term"], -polarity, out, side)
    else:
        for term in predicate["terms"]:
            _positions(term, polarity, out, side)


def positions(raw_policy) -> dict:
    """{(side, attribute, value): {+1, -1}}: the polarities under which each rule side names each value."""
    out = {}
    for rule in raw_policy["rules"]:
        side = rule["effect"]
        _positions(rule["evidence_use"]["predicate"], 1, out, side)
        _positions(rule["release"]["predicate"], 1, out, side)
    return out


def safe_to_add(raw_policy, attribute, value) -> bool:
    """Adding `value` to an item cannot widen: no permit atom names it positively, no deny atom negatively.

    An atom that names the value flips False -> True. In positive position under a permit
    rule that can enable the permit; in negative position under a deny rule it can disable
    the deny. Everywhere else the flip only narrows. Every work-only preset the design
    compiles to names its private values on the deny side positively, which is this case.
    """
    found = positions(raw_policy)
    return 1 not in found.get(("permit", attribute, value), set()) and \
        -1 not in found.get(("deny", attribute, value), set())


def safe_to_remove(raw_policy, attribute, value) -> bool:
    """Removing `value` cannot widen: no deny atom names it positively, no permit atom negatively."""
    found = positions(raw_policy)
    return 1 not in found.get(("deny", attribute, value), set()) and \
        -1 not in found.get(("permit", attribute, value), set())


# --- policy narrowing operations (each returns a narrowed raw policy, or None when not applicable) ------------

def add_deny(raw, predicate, source_selection, table_list, capability):
    narrowed = deepcopy(raw)
    taken = [int(rule["rule_id"].rsplit("-", 1)[1]) for rule in raw["rules"] if rule["rule_id"].startswith("rule-added-deny-")]
    narrowed["rules"].append(source_rule("rule-added-deny-%d" % (max(taken, default=-1) + 1), "deny", sources=source_selection, predicate=predicate,
                                         release_predicate=predicate, ceiling="raw",
                                         forms=[form(capability, table_list)]))
    return narrowed


def _rule_indexes(raw, effect):
    return [index for index, rule in enumerate(raw["rules"]) if rule["effect"] == effect]


def drop_permit(raw, choice):
    permits = _rule_indexes(raw, "permit")
    if not permits:
        return None
    narrowed = deepcopy(raw)
    del narrowed["rules"][permits[choice % len(permits)]]
    return narrowed


def strengthen_permit(raw, choice, extra_predicate):
    permits = _rule_indexes(raw, "permit")
    if not permits:
        return None
    narrowed = deepcopy(raw)
    rule = narrowed["rules"][permits[choice % len(permits)]]
    rule["evidence_use"]["predicate"] = {"kind": "all_of", "terms": [rule["evidence_use"]["predicate"], extra_predicate]}
    return narrowed


def weaken_deny(raw, choice, extra_predicate):
    denies = _rule_indexes(raw, "deny")
    if not denies:
        return None
    narrowed = deepcopy(raw)
    rule = narrowed["rules"][denies[choice % len(denies)]]
    rule["release"]["predicate"] = {"kind": "any_of", "terms": [rule["release"]["predicate"], extra_predicate]}
    return narrowed


def shrink_permit_sources(raw, choice):
    permits = [i for i in _rule_indexes(raw, "permit")
               if raw["rules"][i]["evidence_use"]["sources"]["kind"] == "only" and raw["rules"][i]["evidence_use"]["sources"]["values"]]
    if not permits:
        return None
    narrowed = deepcopy(raw)
    values = narrowed["rules"][permits[choice % len(permits)]]["evidence_use"]["sources"]["values"]
    values.pop(choice % len(values))
    return narrowed


def shrink_permit_tables(raw, choice):
    permits = [i for i in _rule_indexes(raw, "permit") if any(f["tables"] for f in raw["rules"][i]["release"]["forms"])]
    if not permits:
        return None
    narrowed = deepcopy(raw)
    forms = [f for f in narrowed["rules"][permits[choice % len(permits)]]["release"]["forms"] if f["tables"]]
    forms[choice % len(forms)]["tables"].pop(choice % len(forms[choice % len(forms)]["tables"]))
    return narrowed


def raise_permit_ceiling(raw, choice):
    permits = [i for i in _rule_indexes(raw, "permit") if raw["rules"][i]["release"]["ceiling"] == "raw"]
    if not permits:
        return None
    narrowed = deepcopy(raw)
    narrowed["rules"][permits[choice % len(permits)]]["release"]["ceiling"] = "summary"
    return narrowed


def widen_deny_sources(raw, choice):
    denies = [i for i in _rule_indexes(raw, "deny")
              if raw["rules"][i]["evidence_use"]["sources"]["kind"] == "only"
              and set(raw["rules"][i]["evidence_use"]["sources"]["values"]) != set(UNIVERSE)]
    if not denies:
        return None
    narrowed = deepcopy(raw)
    values = narrowed["rules"][denies[choice % len(denies)]]["evidence_use"]["sources"]["values"]
    missing = [s for s in UNIVERSE if s not in values]
    values.append(missing[choice % len(missing)])
    return narrowed


def widen_deny_tables(raw, choice, capability):
    denies = _rule_indexes(raw, "deny")
    if not denies:
        return None
    narrowed = deepcopy(raw)
    rule = narrowed["rules"][denies[choice % len(denies)]]
    rule["release"]["forms"].append(form(capability, LEAF_TABLES))
    return narrowed


NARROWINGS = ("add_deny", "drop_permit", "strengthen_permit", "weaken_deny", "shrink_permit_sources",
              "shrink_permit_tables", "raise_permit_ceiling", "widen_deny_sources", "widen_deny_tables")


def narrow(raw, name, draw, capability):
    """Apply one named narrowing with drawn parameters; None when the policy has nothing to narrow that way."""
    choice = draw(st.integers(0, 7))
    if name == "add_deny":
        return add_deny(raw, draw(predicates()), draw(sources()), draw(tables()), capability)
    if name == "drop_permit":
        return drop_permit(raw, choice)
    if name == "strengthen_permit":
        return strengthen_permit(raw, choice, draw(predicates()))
    if name == "weaken_deny":
        return weaken_deny(raw, choice, draw(predicates()))
    if name == "shrink_permit_sources":
        return shrink_permit_sources(raw, choice)
    if name == "shrink_permit_tables":
        return shrink_permit_tables(raw, choice)
    if name == "raise_permit_ceiling":
        return raise_permit_ceiling(raw, choice)
    if name == "widen_deny_sources":
        return widen_deny_sources(raw, choice)
    if name == "widen_deny_tables":
        return widen_deny_tables(raw, choice, capability)
    raise ValueError(name)


def reviewed_by_floor(evidence: QualifiedEvidence):
    """Run the resolver's review floor over an in-memory review of this evidence's snapshot.

    Exactly the check `_eligible` makes on every read before a decision is computed, with
    the database-backed floors that need rows (owner-only records, tombstones, copies)
    given an empty store, so only the classification-set checks are exercised.
    """
    import sqlite3
    from unittest import mock
    from topos.permissions_v2.evidence import EvidenceResolver, OwnerEvidenceReview
    review = OwnerEvidenceReview(version="topos-owner-evidence-review/v1", review_id=evidence.review_id,
                                 owner_id=BINDING.owner_id, reviewed_at=1100, snapshot=evidence.snapshot,
                                 classifications=evidence.classifications)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entity_blackholes(entity_id TEXT)")
    conn.execute("CREATE TABLE owner_only_records(canonical_table TEXT, record_id TEXT)")
    conn.execute("CREATE TABLE intelligence_exclusions(exclusion_id TEXT, artifact_type TEXT, artifact_key TEXT)")
    resolver = EvidenceResolver.__new__(EvidenceResolver)
    resolver.binding = BINDING
    rows = {_key(ref.identity): {} for ref in evidence.snapshot.artifacts + evidence.snapshot.leaves}
    with mock.patch("topos.permissions_v2.evidence.permit_subjects", return_value={"self"}), \
            mock.patch("topos.permissions_v2.evidence.restriction_subjects", return_value=set()):
        return resolver._eligible(conn, evidence.snapshot, rows, review, contract=evidence.subject_contract)


def fixed_evidence(capability, *, leaves=1, domains=("work",), sensitivity="none"):
    """Deterministic evidence for the witness tests: no drawing, no example database."""
    contract = SUBJECT_CONTRACT_BY_CAPABILITY[capability]
    leaf_ids = [identity("conversation_messages", f"rec-{index}", UNIVERSE[index % len(UNIVERSE)]) for index in range(leaves)]
    root_revision = "1" * 64
    snapshot = EvidenceSnapshot(binding=BINDING, canonical_file_revision="2" * 64, fact_id="fact-1",
                                candidate_revision=root_revision, lineage_revision="3" * 64, protection_revision="4" * 64,
                                artifacts=[EvidenceRevision(identity=identity("signal_objects", "fact-1"), revision=root_revision)],
                                leaves=[EvidenceRevision(identity=leaf, revision=("%02d" % index) * 32)
                                        for index, leaf in enumerate(leaf_ids)])
    classifications = [classification(item, domains, sensitivity) for item in snapshot.artifacts + snapshot.leaves]
    return QualifiedEvidence(family="owner_stated_fact/v1", snapshot=snapshot, review_id="review-1",
                             review_revision="5" * 64, classifications=classifications, subject_contract=contract,
                             execution_enabled=False)
