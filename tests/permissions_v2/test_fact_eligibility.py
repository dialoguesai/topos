"""Differential pure P2b checks against the pinned pre-extraction implementation.

All rows/reviews come from scratch fixtures. No runtime, model or recipient is
configured; a consistent bundle remains insufficient evidence of live authority.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
from itertools import permutations, product
import json
from pathlib import Path
import sqlite3

import pytest

from tests.permissions_v2 import _fact_policy_oracle as oracle
from topos.permissions_v2.evidence import _row_revision as _surface_revision


def _oracle_row_revision(row):
    # The frozen oracle predates per-table review surfaces and pins every
    # column. Only its revision helper is adapted, by the table each fixture
    # row belongs to; its decision logic stays byte-identical to 81c1e9c.
    table = "signal_objects" if "object_id" in row else "conversation_messages" if "dataset_id" in row else "ai_chat_messages"
    return _surface_revision(row, table=table)


oracle._row_revision = _oracle_row_revision
from tests.permissions_v2.test_evidence import corpus, edit
from tests.permissions_v2.test_fact_policy import AS_OF, atom, bundle, policy, rule, timed, two_leaves, utc
from topos.features.facts.store import FactStore
from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest
from topos.permissions_v2.contract import Binding
from topos.permissions_v2.evidence import _key, _row_revision
from topos.permissions_v2.fact_contract import FactPolicyV2
from topos.permissions_v2.fact_eligibility import DenyStructure, PermitStructure, prepare_fact_eligibility
from topos.permissions_v2.fact_policy import fact_projection_decision


def outcome(function, arguments):
    try:
        return ("decision", canonical_bytes(function(**arguments).model_dump()))
    except (PolicyError, ValueError, TypeError, AttributeError) as error:
        return ("error", type(error).__name__, getattr(error, "code", str(error)))


def compare(raw, supplied, **options):
    arguments = dict(policy=FactPolicyV2.parse(raw), **deepcopy(supplied),
        binding=Binding.parse(options.pop("binding", raw["binding"])),
        request_as_of=options.pop("request_as_of", AS_OF), now=options.pop("now", AS_OF))
    assert not options
    expected = outcome(oracle.fact_projection_decision, deepcopy(arguments))
    actual = outcome(fact_projection_decision, arguments)
    assert actual == expected
    return actual


def test_oracle_is_exact_pinned_source_except_its_import_prefix():
    source = Path(oracle.__file__).read_text().split("\n", 3)[3].replace("from topos.permissions_v2.", "from .")
    assert hashlib.sha256(source.encode()).hexdigest() == "bee3dc870812a6e0ab6a05cea49488099973576169063a98eb1354c8d4e16fe1"


@pytest.fixture
def nested(timed):
    """Root -> child -> grandchild with disjoint sibling/descendant sources."""
    with sqlite3.connect(timed[0].path) as conn:
        conn.execute("INSERT INTO conversation_messages(message_id,dataset_id,source_id,content,is_from_self,deleted_at,owner_user_id,event_at) VALUES(?,?,?,?,?,?,?,?)",
            ("message-2", "dataset-2", "source-2", "A synthetic reading statement.", 1, None, "owner-1", utc(AS_OF-20)))
        grandchild = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="reading circle",
            disclosure="scoped", asserted_by="owner", valid_from=utc(AS_OF-100),
            source_refs=[{"table": "conversation_messages", "record_id": "message-2", "source_id": "source-2", "dataset_id": "dataset-2"}])
        child = FactStore(conn).assert_fact(subject_entity_id="self", predicate="member_of", object_value="synthetic group",
            disclosure="scoped", asserted_by="owner", valid_from=utc(AS_OF-100), source_refs=[
                {"table": "signal_objects", "record_id": grandchild["object_id"]},
                {"table": "ai_chat_messages", "record_id": "ai-message-1", "source_id": "ai-source-1"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps([
            {"table": "signal_objects", "record_id": child["object_id"]},
            {"table": "conversation_messages", "record_id": "message-1", "source_id": "source-1", "dataset_id": "dataset-1"}]), timed[2]))
    return timed, child["object_id"]


@pytest.mark.parametrize("event", [None, utc(AS_OF+1), utc(AS_OF-86400, -1), utc(AS_OF-86400), utc(AS_OF-1)])
def test_descendant_selection_time_and_membership_matrix(nested, event):
    data, child = nested
    edit(data, "UPDATE conversation_messages SET event_at=? WHERE message_id='message-2'", (event,))
    edit(data, "UPDATE ai_chat_messages SET event_at=?", (utc(AS_OF-90000),))
    supplied = bundle(data, transform=lambda items: [item.model_copy(update={"domains": ["health"]})
        if item.evidence.identity.record_id == child else item for item in items])
    raw = policy(data)
    raw["source_universe"]["source_ids"].append("source-2")
    raw["rules"][0]["evidence_use"]["sources"]["values"].append("source-2")
    raw["rules"][0]["evidence_use"]["event_window"]["max_age_seconds"] = 172800
    raw["rules"][0]["evidence_use"]["predicate"] = {"kind": "all_of", "terms": []}
    predicate_pairs = [(atom("health"), atom("health")), (atom("missing"), atom("health")),
        (atom("health"), atom("reading")), ({"kind": "not", "term": atom("health")}, {"kind": "any_of", "terms": []})]
    sources = [[], ["source-1"], ["source-2"], ["ai-source-1", "source-2"]]
    tables = [[], ["signal_objects"], ["conversation_messages"], ["signal_objects", "conversation_messages", "ai_chat_messages"]]
    # 128 independent cases per event fixture, including false AND unknown,
    # true OR unknown, absent scopes and source/descendant correlation.
    for selected_sources, selected_tables, window, predicates in product(sources, tables, [86400, 172800], predicate_pairs):
        case = deepcopy(raw)
        denied = rule("deny-synthetic", "deny", "health")
        denied["evidence_use"].update(sources={"kind": "only", "values": selected_sources}, tables=selected_tables,
            predicate=predicates[0])
        denied["evidence_use"]["event_window"]["max_age_seconds"] = window
        denied["release"]["predicate"] = predicates[1]
        case["rules"].append(denied)
        compare(case, supplied)


@pytest.mark.parametrize("start", ["", "2027-01-15", utc(AS_OF+1), utc(AS_OF)])
def test_validity_unknown_and_deny_result_ordering(timed, start):
    edit(timed, "UPDATE signal_objects SET valid_from=?", (start,))
    edit(timed, "UPDATE conversation_messages SET event_at=NULL")
    supplied = bundle(timed)
    for rules in [[], [rule()], [rule("deny", "deny")], [rule(), rule("deny", "deny")]]:
        raw = policy(timed); raw["rules"] = rules
        compare(raw, supplied)


@pytest.mark.parametrize("change,expected", [("missing_start", "indeterminate"), ("missing_end", "indeterminate"),
    ("closed", "deny"), ("future", "deny")])
def test_consistent_pure_bundle_preserves_missing_and_closed_fact_validity(timed, change, expected):
    # Deliberately construct internally consistent pure input to exercise fields
    # a live resolver may withhold sooner. Rehashing is not an owner review; this
    # fixture is never supplied to the release service or a model provider.
    supplied = bundle(timed)
    evidence = supplied["evidence"]; snapshot = evidence.snapshot
    root = snapshot.artifacts[0]; key = _key(root.identity)
    if change == "missing_start": supplied["rows"][key].pop("valid_from")
    elif change == "missing_end": supplied["rows"][key].pop("valid_to")
    elif change == "closed": supplied["rows"][key]["valid_to"] = utc(AS_OF+1)
    else: supplied["rows"][key]["valid_from"] = utc(AS_OF+1)
    replacement = root.model_copy(update={"revision": _row_revision(supplied["rows"][key], table="signal_objects")})
    snapshot = snapshot.model_copy(update={"artifacts": [replacement], "candidate_revision": replacement.revision,
        "lineage_revision": digest({"artifacts": [replacement.model_dump()],
            "leaves": [ref.model_dump() for ref in sorted(snapshot.leaves, key=lambda ref: _key(ref.identity))],
            "edges": {key: sorted(_key(ref.identity) for ref in snapshot.leaves)}})})
    supplied["evidence"] = evidence.model_copy(update={"snapshot": snapshot, "classifications": [
        item.model_copy(update={"evidence": replacement}) if _key(item.evidence.identity) == key else item
        for item in evidence.classifications]})
    projection = supplied["projection"]
    supplied["projection"] = projection.model_copy(update={"candidate": projection.candidate.model_copy(update={"snapshot": snapshot})})
    raw = policy(timed)
    actual = compare(raw, supplied)
    assert actual[0] == "decision" and json.loads(actual[1])["verdict"] == expected
    raw["rules"].append(rule("explicit-deny", "deny"))
    denied = json.loads(compare(raw, supplied)[1])
    assert denied["verdict"] == "deny"
    assert denied["reason_code"] == ("fact_not_current" if change in {"closed", "future"} else "rule_deny")


def test_full_decision_keeps_first_allow_all_denies_and_inference_precedence(timed):
    supplied = bundle(timed)
    first, second = rule("allow-first"), rule("allow-second")
    denied, denied_again = rule("deny-first", "deny"), rule("deny-second", "deny")
    inference = rule("inference"); inference["release"]["ceiling"] = "inference"
    denied_again["release"]["ceiling"] = "inference"
    for ordered in permutations([first, second, denied, denied_again, inference]):
        raw = policy(timed); raw["rules"] = list(ordered)
        compare(raw, supplied)
    for ordered in ([first, second], [second, first], [inference], [first, inference], [inference, first]):
        raw = policy(timed); raw["rules"] = ordered
        compare(raw, supplied)
        result = fact_projection_decision(policy=FactPolicyV2.parse(raw), **supplied,
            binding=Binding.parse(raw["binding"]), request_as_of=AS_OF, now=AS_OF)
        if result.verdict == "permit":
            assert result.matched_allow_clause_ids == [next(item["rule_id"] for item in ordered if item["release"]["ceiling"] != "inference")]


@pytest.mark.parametrize("effect,empty", product(["permit", "deny"], ["sources", "tables", "forms", "processors"]))
def test_empty_structural_bounds_and_all_universe_keep_exact_decisions(timed, effect, empty):
    supplied = bundle(timed)
    raw = policy(timed); selected = rule("selected", effect)
    if empty in {"sources", "processors"}: selected["evidence_use"][empty]["values"] = []
    elif empty == "tables": selected["evidence_use"][empty] = []
    else: selected["release"][empty] = []
    raw["rules"] = [rule(), selected] if effect == "deny" else [selected]
    compare(raw, supplied)
    raw["rules"][0]["evidence_use"]["sources"] = {"kind": "all", "universe_id": "universe-1", "universe_revision": 1, "growth": "require_consent"}
    compare(raw, supplied)


@pytest.mark.parametrize("axis", ["sources", "tables", "predicates"])
def test_partial_permits_are_never_unioned(timed, axis):
    two_leaves(timed); supplied = bundle(timed)
    raw = policy(timed); raw["rules"] = [rule("first"), rule("second")]
    if axis == "sources":
        raw["rules"][0]["evidence_use"]["sources"]["values"] = ["source-1"]
        raw["rules"][1]["evidence_use"]["sources"]["values"] = ["ai-source-1"]
    elif axis == "tables":
        raw["rules"][0]["evidence_use"]["tables"] = ["signal_objects", "conversation_messages"]
        raw["rules"][1]["evidence_use"]["tables"] = ["signal_objects", "ai_chat_messages"]
    else:
        raw["rules"][0]["release"]["predicate"] = atom("health")
        raw["rules"][1]["evidence_use"]["predicate"] = atom("health")
    actual = compare(raw, supplied)
    assert json.loads(actual[1])["verdict"] == "deny"


@pytest.mark.parametrize("bad", ["rows", "binding", "projection", "lineage", "sensitivity", "request_type", "validity"])
def test_complete_error_and_stale_authority_precedence(timed, bad):
    supplied = bundle(timed); raw = policy(timed); options = {"now": AS_OF+121}
    if bad == "rows": next(iter(supplied["rows"].values()))["valid_from"] = "changed"
    elif bad == "binding": options["binding"] = {**raw["binding"], "actor_id": "another"}
    elif bad == "projection": supplied["projection"] = supplied["projection"].model_copy(update={"candidate": supplied["projection"].candidate.model_copy(update={"evidence_review_revision": "f"*64})})
    elif bad == "lineage": supplied["evidence"] = supplied["evidence"].model_copy(update={"snapshot": supplied["evidence"].snapshot.model_copy(update={"lineage_revision": "f"*64})})
    elif bad == "sensitivity": supplied["projection"] = supplied["projection"].model_copy(update={"classification": supplied["projection"].classification.model_copy(update={"sensitivity": "none"})})
    elif bad == "request_type": options["request_as_of"] = True
    else: raw["validity"]["expires_at"] = AS_OF
    outcome_value = compare(raw, supplied, **options)
    assert outcome_value[0] == ("decision" if bad == "validity" else "error")


def guard_predicate_evaluation(patch):
    """Fail on any predicate evaluation: every bound evaluate_predicate and any inline Atom read."""
    import sys
    from topos.permissions_v2 import contract
    original = contract.evaluate_predicate
    sites = [module for name, module in list(sys.modules.items())
             if name.startswith("topos.") and getattr(module, "evaluate_predicate", None) is original]
    for module in sites:
        patch.setattr(module, "evaluate_predicate", lambda *_: pytest.fail("membership evaluated during preparation"))
    def atom_read(self, name):
        if name in contract.Atom.model_fields and name != "kind":
            pytest.fail("membership evaluated during preparation")
        return object.__getattribute__(self, name)
    patch.setattr(contract.Atom, "__getattribute__", atom_read)
    return {module.__name__ for module in sites}


def test_structural_preparation_never_evaluates_membership_and_captures_frozen_masks(timed, monkeypatch):
    from topos.permissions_v2 import contract
    supplied = bundle(timed); raw = policy(timed)
    raw["rules"] = [rule("excluded-by-membership", domain="health"), rule("deny", "deny")]
    parsed = FactPolicyV2.parse(raw)
    with monkeypatch.context() as patch:
        sites = guard_predicate_evaluation(patch)
        assert {"topos.permissions_v2.contract", "topos.permissions_v2.fact_policy"} <= sites
        sentinel = parsed.rules[0].release.predicate
        with pytest.raises(pytest.fail.Exception): contract.evaluate_predicate(sentinel, {"domain": ["health"]})
        with pytest.raises(pytest.fail.Exception): sentinel.values
        checked_policy, _, _, structure = prepare_fact_eligibility(policy=parsed, **supplied,
            binding=Binding.parse(raw["binding"]), request_as_of=AS_OF, now=AS_OF)
    assert [item.rule_index for item in structure.clauses] == [0, 1]
    assert isinstance(structure.clauses[0], PermitStructure) and isinstance(structure.clauses[1], DenyStructure)
    assert structure.clauses[0].leaf_times == (True,)
    parsed.rules.clear()
    for row in supplied["rows"].values(): row["event_at"] = None
    assert len(checked_policy.rules) == 2 and structure.clauses[0].leaf_times == (True,)
    with pytest.raises(FrozenInstanceError): structure.clauses[0].rule_index = 5
    assert not hasattr(structure, "execution_enabled") and not hasattr(structure, "verdict")
