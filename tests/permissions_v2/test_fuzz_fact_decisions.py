"""Fuzz lane, part 5: the fact (p2b) decision, where Unknown arises from time.

Over one real reviewed bundle (the scratch corpus of test_evidence, its fact and leaf
rows) and generated p2b-v1 policies and clocks:

T1  Unknown withholds, decision level. When any leaf's event time is unknown, or the fact's
    own validity is unknown, no policy and no clock ever yields `permit`: the verdict is deny
    or indeterminate, with the unknown named in `missing_context_codes`.
T2  Narrowing is monotone on the fact path: adding a deny rule, dropping a permit rule,
    strengthening a permit predicate, shrinking a permit rule's sources or tables, raising
    its ceiling, shrinking a permit window or growing a deny window never move a verdict
    toward permit.
T3  Stale authority dominates: a clock before the request, more than 120 s after it, or
    outside the policy's validity is a deny for `stale_authority` whatever the rules.
T4  Differential: on p2b-v1 documents the decision equals the frozen pre-extraction oracle
    (tests/permissions_v2/_fact_policy_oracle.py, 81c1e9c) byte for byte, decision or error.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import _fact_policy_oracle as oracle  # noqa: E402
from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2.test_evidence import corpus, edit  # noqa: E402,F401
from tests.permissions_v2.test_fact_policy import AS_OF, bundle, policy as base_policy, timed, utc  # noqa: E402,F401
from topos.permissions_v2.canonical import PolicyError, canonical_bytes  # noqa: E402
from topos.permissions_v2.contract import Binding  # noqa: E402
from topos.permissions_v2.evidence import _row_revision  # noqa: E402
from topos.permissions_v2.fact_contract import FactPolicyV2  # noqa: E402
from topos.permissions_v2.fact_policy import fact_projection_decision  # noqa: E402

pytestmark = [pytest.mark.fuzz]
PURE = settings(max_examples=fz.examples("pure"), suppress_health_check=[HealthCheck.function_scoped_fixture])
DOMAINS = ("reading", "work", "health", "finance")
SOURCES = ("source-1", "ai-source-1")
TABLES = ("signal_objects", "conversation_messages", "ai_chat_messages")
WINDOWS = (1, 60, 3600, 86_400, 10 * 86_400, 400 * 86_400)


def _oracle_row_revision(row):
    table = "signal_objects" if "object_id" in row else "conversation_messages" if "dataset_id" in row else "ai_chat_messages"
    return _row_revision(row, table=table)


oracle._row_revision = _oracle_row_revision


def fact_rule(rule_id, effect, *, sources, tables, window, predicate, release_predicate, ceiling):
    return {"rule_id": rule_id, "effect": effect, "evidence_use": {
        "sources": {"kind": "only", "values": list(sources)}, "tables": list(tables),
        "event_window": {"kind": "rolling", "anchor": "server_request_as_of", "max_age_seconds": window,
                         "event_time_semantics": "canonical_event_time_v1", "missing_or_ambiguous": "withhold",
                         "future": "withhold"},
        "predicate": predicate, "purpose": "owner-stated-fact-projection",
        "processors": {"kind": "only", "values": ["owner-engine-local"]}, "new_records": "include_if_predicate"},
        "release": {"predicate": release_predicate, "ceiling": ceiling, "forms": [
            {"family": "owner_stated_fact", "operation": "read", "view_id": "owner_stated_fact.scalar.v1"}]}}


def fact_atom():
    return st.lists(st.sampled_from(DOMAINS), min_size=0, max_size=2, unique=True).map(lambda v: fz._atom("domain", v))


def fact_predicates():
    return st.recursive(st.one_of(fact_atom(), st.sampled_from(fz.SENSITIVITIES).map(lambda s: fz._atom("sensitivity", [s]))),
                        lambda inner: st.one_of(
                            st.lists(inner, min_size=0, max_size=2).map(lambda t: {"kind": "all_of", "terms": t}),
                            st.lists(inner, min_size=0, max_size=2).map(lambda t: {"kind": "any_of", "terms": t}),
                            inner.map(lambda t: {"kind": "not", "term": t})), max_leaves=4)


@st.composite
def fact_policies(draw, corpus_):
    raw = base_policy(corpus_)
    rules = []
    for index in range(draw(st.integers(0, 3))):
        rules.append(fact_rule(f"rule-{index}", draw(st.sampled_from(["permit", "deny"])),
                               sources=draw(st.lists(st.sampled_from(SOURCES), min_size=0, max_size=2, unique=True)),
                               tables=draw(st.lists(st.sampled_from(TABLES), min_size=0, max_size=3, unique=True)),
                               window=draw(st.sampled_from(WINDOWS)), predicate=draw(fact_predicates()),
                               release_predicate=draw(fact_predicates()),
                               ceiling=draw(st.sampled_from(["summary", "raw", "inference"]))))
    raw["rules"] = rules
    return raw


def decide(raw, supplied, *, request_as_of=AS_OF, now=AS_OF):
    return fact_projection_decision(policy=FactPolicyV2.parse(raw), **supplied, binding=Binding.parse(raw["binding"]),
                                    request_as_of=request_as_of, now=now)


def narrow_fact(raw, name, draw):
    choice = draw(st.integers(0, 7))
    permits = [i for i, r in enumerate(raw["rules"]) if r["effect"] == "permit"]
    denies = [i for i, r in enumerate(raw["rules"]) if r["effect"] == "deny"]
    narrowed = deepcopy(raw)
    if name == "add_deny":
        narrowed["rules"].append(fact_rule("rule-added", "deny",
                                           sources=draw(st.lists(st.sampled_from(SOURCES), min_size=1, max_size=2, unique=True)),
                                           tables=draw(st.lists(st.sampled_from(TABLES), min_size=1, max_size=3, unique=True)),
                                           window=draw(st.sampled_from(WINDOWS)), predicate=draw(fact_predicates()),
                                           release_predicate=draw(fact_predicates()), ceiling="summary"))
        return narrowed
    if name == "drop_permit":
        if not permits:
            return None
        del narrowed["rules"][permits[choice % len(permits)]]
        return narrowed
    if name == "strengthen_permit":
        if not permits:
            return None
        rule = narrowed["rules"][permits[choice % len(permits)]]
        rule["evidence_use"]["predicate"] = {"kind": "all_of", "terms": [rule["evidence_use"]["predicate"], draw(fact_predicates())]}
        return narrowed
    if name == "shrink_permit_sources":
        candidates = [i for i in permits if raw["rules"][i]["evidence_use"]["sources"]["values"]]
        if not candidates:
            return None
        values = narrowed["rules"][candidates[choice % len(candidates)]]["evidence_use"]["sources"]["values"]
        values.pop(choice % len(values))
        return narrowed
    if name == "shrink_permit_tables":
        candidates = [i for i in permits if raw["rules"][i]["evidence_use"]["tables"]]
        if not candidates:
            return None
        values = narrowed["rules"][candidates[choice % len(candidates)]]["evidence_use"]["tables"]
        values.pop(choice % len(values))
        return narrowed
    if name == "raise_permit_ceiling":
        candidates = [i for i in permits if raw["rules"][i]["release"]["ceiling"] != "inference"]
        if not candidates:
            return None
        narrowed["rules"][candidates[choice % len(candidates)]]["release"]["ceiling"] = "inference"
        return narrowed
    if name == "shrink_permit_window":
        candidates = [i for i in permits if raw["rules"][i]["evidence_use"]["event_window"]["max_age_seconds"] > 1]
        if not candidates:
            return None
        window = narrowed["rules"][candidates[choice % len(candidates)]]["evidence_use"]["event_window"]
        window["max_age_seconds"] = max(1, window["max_age_seconds"] // (2 + choice))
        return narrowed
    if name == "grow_deny_window":
        if not denies:
            return None
        window = narrowed["rules"][denies[choice % len(denies)]]["evidence_use"]["event_window"]
        window["max_age_seconds"] = min(2**53 - 1, window["max_age_seconds"] * (2 + choice))
        return narrowed
    raise ValueError(name)


FACT_NARROWINGS = ("add_deny", "drop_permit", "strengthen_permit", "shrink_permit_sources", "shrink_permit_tables",
                   "raise_permit_ceiling", "shrink_permit_window", "grow_deny_window")


@pytest.fixture
def known(timed):
    """Every leaf has a UTC event time and the fact a valid_from: time is known everywhere."""
    return timed, bundle(timed)


@pytest.fixture
def unknown_time(corpus):
    """No event_at column, and a valid_from outside the UTC grammar: every time is Unknown."""
    edit(corpus, "UPDATE signal_objects SET valid_from='2027-01-10 08:00'")
    return corpus, bundle(corpus)


@PURE
@given(st.data())
def test_T1_unknown_time_never_permits(unknown_time, data):
    corpus_, supplied = unknown_time
    raw = data.draw(fact_policies(corpus_))
    as_of = data.draw(st.integers(AS_OF - 3000, AS_OF + 3000))
    decision = decide(raw, supplied, request_as_of=as_of, now=as_of + data.draw(st.integers(0, 120)))
    assert decision.verdict != "permit"
    if decision.reason_code == "unknown_context":
        assert decision.missing_context_codes and set(decision.missing_context_codes) <= {"fact_validity", "time"}


@PURE
@given(st.data())
def test_T2_every_fact_policy_narrowing_is_monotone(known, data):
    corpus_, supplied = known
    raw = data.draw(fact_policies(corpus_))
    name = data.draw(st.sampled_from(FACT_NARROWINGS))
    narrowed = narrow_fact(raw, name, data.draw)
    assume(narrowed is not None)
    before, after = decide(raw, supplied), decide(narrowed, supplied)
    assert fz.at_most(after.verdict, before.verdict), (name, before.verdict, after.verdict)


@PURE
@given(st.data())
def test_T3_stale_authority_dominates_every_rule(known, data):
    corpus_, supplied = known
    raw = data.draw(fact_policies(corpus_))
    kind = data.draw(st.sampled_from(["before_request", "too_late", "before_validity", "after_validity"]))
    if kind == "before_request":
        as_of, now = AS_OF, AS_OF - data.draw(st.integers(1, 500))
    elif kind == "too_late":
        as_of, now = AS_OF, AS_OF + data.draw(st.integers(121, 5000))
    elif kind == "before_validity":
        as_of = raw["validity"]["starts_at"] - data.draw(st.integers(1, 500))
        now = as_of
    else:
        as_of = raw["validity"]["expires_at"] + data.draw(st.integers(0, 500))
        now = as_of
    decision = decide(raw, supplied, request_as_of=as_of, now=now)
    assert (decision.verdict, decision.reason_code) == ("deny", "stale_authority")


def outcome(function, arguments):
    try:
        return ("decision", canonical_bytes(function(**arguments).model_dump()))
    except (PolicyError, ValueError, TypeError, AttributeError) as error:
        return ("error", type(error).__name__, getattr(error, "code", str(error)))


@PURE
@given(st.data())
def test_T4_the_decision_equals_the_frozen_oracle_on_every_generated_v1_document(known, data):
    corpus_, supplied = known
    raw = data.draw(fact_policies(corpus_))
    as_of = data.draw(st.integers(AS_OF - 4000, AS_OF + 4000))
    now = as_of + data.draw(st.integers(-10, 200))
    arguments = dict(policy=FactPolicyV2.parse(raw), **deepcopy(supplied), binding=Binding.parse(raw["binding"]),
                     request_as_of=as_of, now=now)
    assert outcome(fact_projection_decision, arguments) == outcome(oracle.fact_projection_decision, deepcopy(arguments))
