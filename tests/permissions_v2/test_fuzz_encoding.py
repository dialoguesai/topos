"""Fuzz lane, part 2: one canonical encoding (design §3.1 hashes; bookkeeping batch 5 E1).

C1  Canonical JSON is a bijection on its grammar: parse(encode(v)) == v, encode is idempotent,
    two unequal values never share bytes, and a digest is the digest of those bytes.
C2  Everything outside the grammar is refused at the edge, never coerced: floats, integers
    past 2^53-1, non-ASCII keys, surrogates, tuples, bytes, and raw JSON with duplicate keys.
C3  Every policy grammar has exactly one encoding of a document: key order and whitespace in
    the raw JSON do not reach the bytes, parse(dump(parse(raw))) is a fixed point, the
    registry returns the class the capability names and refuses the document under any
    other class, and an undeclared read budget is one encoding -- the key is absent from
    the bytes and an explicit null is refused -- while a declared one is inside the hash.
C4  Opaque record ids: 66 characters, a pure function of (key, grant, table, source, dataset,
    record), stable within a grant, and a key of any other length is refused.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("hypothesis")
from hypothesis import assume, given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2.message_search_corpus import search_policy  # noqa: E402
from tests.permissions_v2.test_contract_and_ledger import sample_policy  # noqa: E402
from topos.permissions_v2.canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest, parse_json  # noqa: E402
from topos.permissions_v2.contract import PolicyV2  # noqa: E402
from topos.permissions_v2.fact_contract import (AttestedSubjectFactPolicy, FactPolicyV2,  # noqa: E402
    StatedDayFactPolicy, WorkFactPolicy)
from topos.permissions_v2.opaque_ids import opaque_record_id  # noqa: E402
from topos.permissions_v2.registry import (AttestedSubjectSourcePolicy, OpaqueSubjectSourcePolicy,  # noqa: E402
    parse_policy)
from topos.permissions_v2.search_contract import SearchPolicy  # noqa: E402

pytestmark = [pytest.mark.fuzz]
PURE = settings(max_examples=fz.examples("pure"))
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2"

ascii_keys = st.text(alphabet=st.characters(min_codepoint=1, max_codepoint=127), min_size=0, max_size=8)
scalars = st.one_of(st.none(), st.booleans(), st.integers(-MAX_INTEGER, MAX_INTEGER), st.text(max_size=12))
json_values = st.recursive(scalars, lambda inner: st.one_of(st.lists(inner, max_size=4),
                                                            st.dictionaries(ascii_keys, inner, max_size=4)),
                           max_leaves=12)


# --- C1, C2 ----------------------------------------------------------------------------------------

@PURE
@given(json_values)
def test_C1_canonical_json_is_a_bijection_on_its_grammar(value):
    encoded = canonical_bytes(value)
    assert parse_json(encoded) == value
    assert canonical_bytes(parse_json(encoded)) == encoded
    assert digest(value) == digest(parse_json(encoded))
    assert encoded.isascii()


@PURE
@given(json_values, json_values)
def test_C1_unequal_values_never_share_bytes(left, right):
    assume(left != right)
    assert canonical_bytes(left) != canonical_bytes(right)
    assert digest(left) != digest(right)


@PURE
@given(st.one_of(
    st.floats(allow_nan=False, allow_infinity=False).map(lambda f: {"x": f}),
    st.integers(MAX_INTEGER + 1, MAX_INTEGER * 4).map(lambda i: {"x": i}),
    st.integers(MAX_INTEGER + 1, MAX_INTEGER * 4).map(lambda i: {"x": -i}),
    st.text(alphabet=st.characters(min_codepoint=128, max_codepoint=0x2FFF), min_size=1, max_size=4).map(lambda k: {k: 1}),
    st.just({"x": (1, 2)}), st.just({"x": b"bytes"}), st.just({"x": {1: 2}}),
    st.just({"x": "\ud800"}), st.just({"x": "a\udfffb"})))
def test_C2_values_outside_the_grammar_are_refused_not_coerced(value):
    with pytest.raises(PolicyError):
        canonical_bytes(value)


@PURE
@given(st.sampled_from([b'{"x":1,"x":2}', b'{"x":1.0}', b'{"x":1e0}', b'{"x":NaN}', b'{"x":-Infinity}',
                        b'{"x":9007199254740992}', b'{"x":"\\ud800"}', b'\xff', b"[" * 50 + b"0" + b"]" * 50,
                        b'{"\xc3\xa9":1}', b"{}x", b"", b"nul"]))
def test_C2_non_canonical_raw_json_is_refused(raw):
    with pytest.raises(PolicyError):
        parse_json(raw)


# --- C3: every policy class -----------------------------------------------------------------------

def _p2b(capability, evaluator, extra_versions, work=False):
    raw = deepcopy(json.loads((FIXTURES / "fact_policy" / "signed-golden-v1.json").read_text())["policy"])
    raw["versions"] = {"vocabulary": "owner-review-vocabulary/v1", "capability": capability, **extra_versions}
    raw["evaluator"] = {"kind": "hard_rules", "version": evaluator}
    if work:
        for rule in raw["rules"]:
            rule["release"]["forms"] = [{"family": "owner_stated_work", "operation": "read",
                                         "view_id": "owner_stated_work.scalar.v1"}]
    return raw


EXACT = {"semantics": "exact_instant_v1", "precision": "instant", "instants": "explicit_utc_exact", "unknown": "withhold"}
STATED = {"semantics": "stated_day_v1", "precision": "day", "timezone_basis": "unrecorded_any_earth_offset",
          "current_from": "next_day_12_00_utc", "instants": "explicit_utc_exact", "unknown": "withhold",
          "not_elapsed": "withhold"}
WORK_FAMILY = {"name": "owner_stated_work", "view_id": "owner_stated_work.scalar.v1", "predicate": "works_at",
               "assertion": "explicit_atomic_work_engagement",
               "producer": "first_person_present_tense_message_statement_v1",
               "projection_version": "exact-owner-work/v1", "other_predicates": "withhold"}


def documents():
    """One raw document per policy grammar, each valid today."""
    p2a_v3 = fz.source_policy(fz.CAPABILITY_OPAQUE, [fz.source_rule("r", "permit", sources=dict(fz.ALL_SOURCES),
        predicate=fz._atom("domain", ["work"]), release_predicate=fz._atom("domain", ["work"]), ceiling="raw",
        forms=[fz.form(fz.CAPABILITY_OPAQUE, fz.LEAF_TABLES)])])
    p2a_v2 = fz.source_policy(fz.CAPABILITY_ATTESTED, [fz.source_rule("r", "deny", sources=dict(fz.ALL_SOURCES),
        predicate=fz._atom("sensitivity", ["special"]), release_predicate=fz._atom("sensitivity", ["special"]),
        ceiling="raw", forms=[fz.form(fz.CAPABILITY_ATTESTED, fz.LEAF_TABLES)])])
    return {
        "permissions-beta/p2a-v1": (PolicyV2, sample_policy()),
        "permissions-beta/p2a-v2": (AttestedSubjectSourcePolicy, p2a_v2),
        "permissions-beta/p2a-v3": (OpaqueSubjectSourcePolicy, p2a_v3),
        "permissions-beta/p2b-v1": (FactPolicyV2, _p2b("permissions-beta/p2b-v1", "hard-rules/p2b-v1", {})),
        "permissions-beta/p2b-v2": (StatedDayFactPolicy, _p2b("permissions-beta/p2b-v2", "hard-rules/p2b-v2",
                                                               {"fact_validity": STATED})),
        "permissions-beta/p2b-v3": (AttestedSubjectFactPolicy, _p2b("permissions-beta/p2b-v3", "hard-rules/p2b-v3",
                                                                     {"fact_validity": EXACT, "subject_binding": fz.SUBJECT_BINDING})),
        "permissions-beta/p2b-v4": (WorkFactPolicy, _p2b("permissions-beta/p2b-v4", "hard-rules/p2b-v4",
                                                          {"fact_validity": STATED, "subject_binding": fz.SUBJECT_BINDING,
                                                           "output_family": WORK_FAMILY}, work=True)),
        "permissions-beta/p2c-v1": (SearchPolicy, search_policy()),
    }


DOCUMENTS = documents()
CLASSES = {model for model, _ in DOCUMENTS.values()}


def shuffled(value, draw):
    """The same JSON value with every object's keys in a drawn order."""
    if isinstance(value, dict):
        keys = list(value)
        order = draw(st.permutations(keys))
        return {key: shuffled(value[key], draw) for key in order}
    if isinstance(value, list):
        return [shuffled(item, draw) for item in value]
    return value


@PURE
@given(st.sampled_from(sorted(DOCUMENTS)), st.data())
def test_C3_one_document_one_encoding_whatever_the_raw_layout(capability, data):
    model, raw = DOCUMENTS[capability]
    reference = canonical_bytes(model.parse(raw).model_dump())
    layout = shuffled(raw, data.draw)
    indent = data.draw(st.sampled_from([None, 1, 4]))
    text = json.dumps(layout, indent=indent, ensure_ascii=data.draw(st.booleans()))
    parsed = model.parse(text)
    assert canonical_bytes(parsed.model_dump()) == reference
    assert canonical_bytes(model.parse(parsed.model_dump()).model_dump()) == reference
    assert digest(parsed.model_dump()) == digest(model.parse(raw).model_dump())


@PURE
@given(st.sampled_from(sorted(DOCUMENTS)), st.sampled_from(sorted(DOCUMENTS)))
def test_C3_the_registry_returns_the_capabilitys_class_and_no_other_class_accepts_the_document(mine, theirs):
    model, raw = DOCUMENTS[mine]
    assert type(parse_policy(raw)) is model
    other_model = DOCUMENTS[theirs][0]
    if other_model is not model:
        with pytest.raises(PolicyError):
            other_model.parse(raw)


@PURE
@given(st.sampled_from(sorted(DOCUMENTS)), st.integers(1, MAX_INTEGER))
def test_C3_an_undeclared_budget_is_one_encoding_and_a_declared_one_is_in_the_hash(capability, budget):
    model, raw = DOCUMENTS[capability]
    plain = model.parse(raw)
    assert plain.read_budget_per_day is None
    assert b"read_budget_per_day" not in canonical_bytes(plain.model_dump())
    with pytest.raises(PolicyError):
        model.parse({**raw, "read_budget_per_day": None})
    declared = model.parse({**raw, "read_budget_per_day": budget})
    assert declared.read_budget_per_day == budget
    assert b'"read_budget_per_day":%d' % budget in canonical_bytes(declared.model_dump())
    assert digest(declared.model_dump()) != digest(plain.model_dump())
    assert model.parse(declared.model_dump()).model_dump() == declared.model_dump()


@PURE
@given(st.sampled_from(sorted(DOCUMENTS)), st.one_of(st.just(0), st.just(-1), st.just(MAX_INTEGER + 1),
                                                     st.booleans(), st.floats(allow_nan=False), st.text(max_size=4),
                                                     st.lists(st.integers(), max_size=2)))
def test_C3_a_budget_outside_the_grammar_is_refused(capability, value):
    model, raw = DOCUMENTS[capability]
    with pytest.raises(PolicyError):
        model.parse({**raw, "read_budget_per_day": value})


@PURE
@given(st.sampled_from(sorted(DOCUMENTS)), st.data())
def test_C3_a_document_with_an_unknown_key_anywhere_is_refused(capability, data):
    model, raw = DOCUMENTS[capability]
    target = deepcopy(raw)
    path = data.draw(st.sampled_from(["", "binding", "versions", "validity", "source_universe", "hard_constraints",
                                      "evaluator"]))
    node = target if not path else target[path]
    node[data.draw(st.sampled_from(["x", "natural_language_hint", "ignore_floors", "_"]))] = data.draw(scalars)
    with pytest.raises(PolicyError):
        model.parse(target)


# --- C4: opaque ids ----------------------------------------------------------------------------------

@PURE
@given(st.binary(min_size=32, max_size=32), fz.identifiers, st.sampled_from(fz.LEAF_TABLES),
       st.one_of(st.none(), fz.identifiers), st.one_of(st.none(), fz.identifiers), fz.identifiers)
def test_C4_an_opaque_id_is_a_pure_66_character_function_of_its_inputs(key, grant, table, source, dataset, record):
    first = opaque_record_id(key, grant_id=grant, table=table, source_id=source, dataset_id=dataset, record_id=record)
    second = opaque_record_id(key, grant_id=grant, table=table, source_id=source, dataset_id=dataset, record_id=record)
    assert first == second and len(first) == 66 and first.startswith("r.")
    assert all(char in "0123456789abcdef" for char in first[2:])


@PURE
@given(st.binary(min_size=32, max_size=32), st.binary(min_size=32, max_size=32), fz.identifiers, fz.identifiers, fz.identifiers)
def test_C4_another_key_or_grant_or_record_gives_another_id(key, other_key, grant, other_grant, record):
    base = opaque_record_id(key, grant_id=grant, table="conversation_messages", source_id="s", dataset_id="d", record_id=record)
    if other_key != key:
        assert base != opaque_record_id(other_key, grant_id=grant, table="conversation_messages", source_id="s",
                                        dataset_id="d", record_id=record)
    if other_grant != grant:
        assert base != opaque_record_id(key, grant_id=other_grant, table="conversation_messages", source_id="s",
                                        dataset_id="d", record_id=record)
    assert base != opaque_record_id(key, grant_id=grant, table="ai_chat_messages", source_id="s", dataset_id=None,
                                    record_id=record)


@PURE
@given(st.binary(min_size=0, max_size=64).filter(lambda k: len(k) != 32))
def test_C4_a_key_of_the_wrong_length_is_refused(key):
    with pytest.raises(PolicyError) as refused:
        opaque_record_id(key, grant_id="g", table="conversation_messages", source_id="s", dataset_id="d", record_id="r")
    assert refused.value.code == "record_key_invalid"
