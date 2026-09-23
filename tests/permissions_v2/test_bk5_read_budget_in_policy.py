"""E1: the owner's daily read budget, declared inside the signed policy.

Bookkeeping batch 4 put a per-grant daily read budget in the control plane and had
to sign it beside the assignment, because the policy grammar is byte-parity-tested
against this module and every `policy_hash` in the wild is a digest of it
(BOOKKEEPING_BATCH_4.md §2 "Where the declaration lives, and why"). This adds the
number to the grammar itself.

The one property that makes that safe: a policy that declares nothing must encode
exactly the bytes it always did. So the field is optional and the model serializer
omits the key when it is None -- not `exclude_none`, which would also drop the
required `natural_language: null` and move every hash. The golden vectors below are
the three that shipped; they are asserted unchanged, which is the real regression
test for every pinned `policy_hash`, signature and frontend fixture.

Declared, the number is inside the canonical bytes and therefore inside the hash.
That is the point of E1: the budget the coordinator enforces is the budget the
owner signed, and changing it is a new policy version, not an edit to a row.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from tests.permissions_v2.message_search_corpus import search_policy
from tests.permissions_v2.source_attested_schemas import export
from tests.permissions_v2.test_contract_and_ledger import owner, sample_policy  # noqa: F401 (fixture)
from topos.permissions_v2.canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest
from topos.permissions_v2.contract import PolicyV2
from topos.permissions_v2.fact_contract import (AttestedSubjectFactPolicy, FactPolicyV2, StatedDayFactPolicy,
    WorkFactPolicy)
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.protocol import SignedMutation
from topos.permissions_v2.registry import AttestedSubjectSourcePolicy, OpaqueSubjectSourcePolicy, parse_policy
from topos.permissions_v2.search_contract import SearchPolicy

pytestmark = [pytest.mark.private]

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2"
# `digest(SearchPolicy.parse(search_policy()).model_dump())` on night/d-engine 7563330b,
# before this field existed. p2c-v1 ships no golden vector of its own, so its
# unchanged-bytes property is pinned here by value.
SEARCH_POLICY_HASH_BEFORE_E1 = "b4bc996d95713df1d48c670aad774beb64082491ba9208cd221eedf65fb967aa"
# Every class that must carry the declaration, including the ones that inherit it.
BUDGETED = (PolicyV2, AttestedSubjectSourcePolicy, OpaqueSubjectSourcePolicy, FactPolicyV2, StatedDayFactPolicy,
            AttestedSubjectFactPolicy, WorkFactPolicy, SearchPolicy)
GOLDEN = ("golden-v1.json", "fact_policy/signed-golden-v1.json", "fact_policy/signed-golden-v2.json")
# Every checked-in export that carries one of the eight classes above.
EXPORTED = {"PolicyV2.schema.json": PolicyV2, "SignedMutation.schema.json": SignedMutation,
            "message_search/SearchPolicy.schema.json": SearchPolicy,
            "source_attested/AttestedSubjectSourcePolicy.schema.json": AttestedSubjectSourcePolicy,
            "source_opaque/OpaqueSubjectSourcePolicy.schema.json": OpaqueSubjectSourcePolicy,
            "fact_policy/FactPolicyV2.schema.json": FactPolicyV2,
            "fact_policy/AttestedSubjectFactPolicy.schema.json": AttestedSubjectFactPolicy,
            "fact_policy/StatedDayFactPolicy.schema.json": StatedDayFactPolicy,
            "fact_policy/WorkFactPolicy.schema.json": WorkFactPolicy}


def golden_policy(name):
    return json.loads((FIXTURES / name).read_text())["policy"]


def policies():
    """One raw document per grammar this batch touches, each already valid today."""
    return {"p2a": (PolicyV2, sample_policy()),
            "p2b": (FactPolicyV2, golden_policy("fact_policy/signed-golden-v1.json")),
            "p2c": (SearchPolicy, search_policy())}


# --- an undeclared budget is not in the bytes ------------------------------------

@pytest.mark.parametrize("name", sorted(policies()))
def test_an_undeclared_budget_leaves_the_canonical_bytes_untouched(name):
    model, raw = policies()[name]
    parsed = model.parse(raw)
    assert parsed.read_budget_per_day is None
    assert "read_budget_per_day" not in parsed.model_dump()
    assert b"read_budget_per_day" not in canonical_bytes(parsed.model_dump())


@pytest.mark.parametrize("name", GOLDEN)
def test_every_shipped_golden_policy_still_hashes_to_its_pinned_value(name):
    golden = json.loads((FIXTURES / name).read_text())
    policy = parse_policy(golden["policy"])
    assert canonical_bytes(policy.model_dump()).decode("ascii") == golden["policy_canonical"]
    assert digest(policy.model_dump()) == golden["policy_hash"]
    assert policy.read_budget_per_day is None


def test_the_search_grammar_hashes_what_it_hashed_before_this_field_existed():
    assert digest(SearchPolicy.parse(search_policy()).model_dump()) == SEARCH_POLICY_HASH_BEFORE_E1


@pytest.mark.parametrize("model", BUDGETED, ids=lambda model: model.__name__)
def test_every_policy_class_carries_the_optional_declaration(model):
    assert "read_budget_per_day" in model.model_fields
    assert model.model_fields["read_budget_per_day"].default is None


# --- a declared budget is in the bytes, and in the hash --------------------------

@pytest.mark.parametrize("name", sorted(policies()))
def test_a_declared_budget_round_trips_and_moves_the_hash(name):
    model, raw = policies()[name]
    before = digest(model.parse(raw).model_dump())
    declared = model.parse({**raw, "read_budget_per_day": 2_500})
    assert declared.read_budget_per_day == 2_500
    assert digest(declared.model_dump()) != before
    assert model.parse(declared.model_dump()).model_dump() == declared.model_dump()
    assert digest(model.parse(declared.model_dump()).model_dump()) == digest(declared.model_dump())


@pytest.mark.parametrize("name", sorted(policies()))
def test_two_different_budgets_are_two_different_policies(name):
    model, raw = policies()[name]
    assert digest(model.parse({**raw, "read_budget_per_day": 1}).model_dump()) != \
           digest(model.parse({**raw, "read_budget_per_day": 2}).model_dump())


@pytest.mark.parametrize("name", sorted(policies()))
@pytest.mark.parametrize("value", [0, -1, MAX_INTEGER + 1, True, False, 1.0, "2500", [], {}])
def test_a_budget_outside_the_range_is_refused_at_construction(name, value):
    model, raw = policies()[name]
    with pytest.raises(PolicyError):
        model.parse({**raw, "read_budget_per_day": value})


@pytest.mark.parametrize("name", sorted(policies()))
@pytest.mark.parametrize("value", [1, 10_000, MAX_INTEGER])
def test_the_whole_declared_range_is_accepted(name, value):
    model, raw = policies()[name]
    assert model.parse({**raw, "read_budget_per_day": value}).read_budget_per_day == value


@pytest.mark.parametrize("name", sorted(policies()))
def test_an_explicit_null_is_refused_so_one_undeclared_policy_has_one_encoding(name):
    """`{"read_budget_per_day": null}` would encode as the absent key and hash as it.

    Two documents with one hash is exactly what a signed grammar must not have:
    the node recomputes `digest(policy.model_dump())` and compares it to the hash
    the control plane signed, so the explicit form would be stored and then
    refused later as `policy_integrity`. Refuse it at parse instead.
    """
    model, raw = policies()[name]
    with pytest.raises(PolicyError, match="schema_invalid"):
        model.parse({**raw, "read_budget_per_day": None})


# --- the declaration travels with the policy, through the parsers ----------------

def test_the_registry_parser_keeps_the_declaration_on_every_capability():
    for _, (model, raw) in sorted(policies().items()):
        parsed = parse_policy({**raw, "read_budget_per_day": 77})
        assert type(parsed) is model and parsed.read_budget_per_day == 77


def test_the_node_ledger_stores_and_reloads_a_declared_budget(budgeted_ledger):
    """A grant activated with a declared budget keeps it through the integrity check.

    `_policy` refuses a stored policy whose `digest(model_dump())` is not the hash
    the activation recorded, so this also proves the declaration is inside the hash
    the node keeps, not beside it.
    """
    ledger, raw = budgeted_ledger
    with ledger._transaction() as conn:
        policy = ledger._policy(conn, raw["policy_version_id"])
    assert policy.read_budget_per_day == 2_500
    assert digest(policy.model_dump()) == digest(PolicyV2.parse(raw).model_dump())


def test_the_authority_the_node_signs_carries_the_budgeted_hash(budgeted_ledger):
    """The authority binding's `policy_hash` is the digest of the declared policy.

    So a recipient's envelope is bound to the budget the owner signed: changing the
    number changes the hash, which stales every envelope issued under the old one.
    """
    ledger, raw = budgeted_ledger
    with ledger._transaction() as conn:
        authority, policy = ledger._authority(conn, raw["binding"]["grant_id"], 1100)
    assert authority.policy_hash == digest(PolicyV2.parse(raw).model_dump())
    assert authority.policy_hash != digest(PolicyV2.parse({k: v for k, v in raw.items()
                                                           if k != "read_budget_per_day"}).model_dump())
    assert policy.read_budget_per_day == 2_500


@pytest.fixture
def budgeted_ledger(tmp_path, owner):  # noqa: F811 (fixture)
    raw = {**sample_policy(), "read_budget_per_day": 2_500}
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    keys = {"beta-key-1": key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    identity = NodeIdentity.parse({name: value for name, value in raw["binding"].items()
                                   if name in NodeIdentity.model_fields})
    ledger = PolicyLedger(tmp_path / "policy-v2.db", identity=identity, protection_revision="a" * 64,
                          trusted_keys=keys)
    ledger.activate(raw, grant_generation=1, assignment_generation=1, expected_epoch=0,
                    command_id="activate-1", now=1100)
    return ledger, raw


# --- the schema exports move by exactly one optional property --------------------

# sha256 of every export that carries a policy, as it stood at night/d-engine 7563330b.
# The test below strips `read_budget_per_day` out of every `properties` block and
# reproduces these bytes, which says the exports changed in this one way and no other:
# no removed property, no moved required list, no re-titled model. The p2a-v1 freeze
# (`test_source_release_attested.FROZEN_V1`) carries the new PolicyV2 value; this
# carries the old one, so the freeze still says what it always said.
EXPORTS_BEFORE_E1 = {
    "PolicyV2.schema.json": "4f864be491ef8d23486fa1faca6458218d5bbd5a5295de96dd227a4bdcf4273f",
    "SignedMutation.schema.json": "2fe4c6e93ed63dd381658af5b8a6a257a5f745e5b43bb543fb180ad910aac064",
    "message_search/SearchPolicy.schema.json": "561b02c7d2c2407d86a1614921640b6d6194f18720fb225cde48890f0090d7aa",
    "source_attested/AttestedSubjectSourcePolicy.schema.json": "bb30ec166ca611c4ec18f5c7e71359322b5d2094e961425875729bc9d197f737",
    "source_opaque/OpaqueSubjectSourcePolicy.schema.json": "0a4461ec9531877dffe0708fd6d1d6d6b260f8cea1c3869c764eba3837527353",
    "fact_policy/FactPolicyV2.schema.json": "0c7eea58588a8097ae72a6dd05aae949f5578cf6b9ad917e6db64cf0f351445f",
    "fact_policy/AttestedSubjectFactPolicy.schema.json": "094724cb73df97e9d6c6b19d563441bd594f6eca52175fae2271411fdacee33e",
    "fact_policy/StatedDayFactPolicy.schema.json": "82e40451ee38fbbe71f1e13fbfbb227c1d9e7ba191af09f7f705faf8eb5a1813",
    "fact_policy/WorkFactPolicy.schema.json": "b30a2108d994b14a818293f4772e922c7942affb3172f42126d72a2a3e276f21",
}


def without_budget(value):
    """The same schema with `read_budget_per_day` gone from every `properties` block."""
    if isinstance(value, dict):
        stripped = {key: without_budget(item) for key, item in value.items() if key != "properties"}
        if "properties" in value:
            stripped["properties"] = {key: without_budget(item) for key, item in value["properties"].items()
                                      if key != "read_budget_per_day"}
        return stripped
    return [without_budget(item) for item in value] if isinstance(value, list) else value


@pytest.mark.parametrize("name", sorted(EXPORTS_BEFORE_E1))
def test_every_moved_export_moved_by_exactly_this_one_optional_property(name):
    current = json.loads((FIXTURES / name).read_text())
    assert "read_budget_per_day" in json.dumps(current), name
    restored = (json.dumps(without_budget(current), indent=2, sort_keys=True) + "\n").encode("ascii")
    assert hashlib.sha256(restored).hexdigest() == EXPORTS_BEFORE_E1[name], name


def test_no_other_export_moved_at_all():
    """Every schema fixture that names no policy is byte-identical to what shipped."""
    moved = {FIXTURES / name for name in EXPORTS_BEFORE_E1}
    for path in sorted(FIXTURES.rglob("*.schema.json")):
        if path not in moved:
            assert b"read_budget_per_day" not in path.read_bytes(), path.name


@pytest.mark.parametrize("name", sorted(EXPORTS_BEFORE_E1))
def test_the_checked_in_export_is_the_model_it_claims_to_export(name):
    assert export(EXPORTED[name]) == (FIXTURES / name).read_bytes()
