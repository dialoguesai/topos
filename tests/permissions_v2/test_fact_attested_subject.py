"""p2b-v3 end to end: a node with several self rows releases what the owner attested.

This is the case the first fact family could not serve. The corpus fact's
subject is an entity id, the node holds more than one `is_self` row, and the
pre-binding rule refuses on both counts. Under a v3 grant, and only after the
owner attests that entity, the same fact releases, and the output still says
`self`.
"""
from copy import deepcopy
import json
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, owner, edit, attest, payload as change_fact
from tests.permissions_v2.test_fact_policy import AS_OF, policy, timed, utc
from tests.permissions_v2.test_fact_release import dispatch, fact_setup, issue, projection_service
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, do_attest
from tests.permissions_v2.test_projection_reviews import prepare
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence_reviews import EvidenceLookup
from topos.permissions_v2.fact_contract import (AttestedSubjectFactDecision, AttestedSubjectFactPolicy,
    CAPABILITY_ATTESTED, EVALUATOR_ATTESTED)
from topos.permissions_v2.identity import ATTESTED_CONTRACT, LEGACY_CONTRACT
from topos.permissions_v2.projection_reviews import RecordProjectionReview
from topos.permissions_v2.registry import parse_policy

EXACT_INSTANT = {"semantics": "exact_instant_v1", "precision": "instant", "instants": "explicit_utc_exact",
                 "unknown": "withhold"}
STATED_DAY = {"semantics": "stated_day_v1", "precision": "day", "timezone_basis": "unrecorded_any_earth_offset",
              "current_from": "next_day_12_00_utc", "instants": "explicit_utc_exact", "unknown": "withhold",
              "not_elapsed": "withhold"}
SUBJECT_BINDING = {"contract": "owner_attested_v1", "statement_version": "owner-identity-attestation/v1",
                   "subjects": "owner_attested_entities", "unattested": "withhold",
                   "moved_since_attestation": "withhold", "rekeyed_facts": "withhold",
                   "literal_self_when_shadowed": "withhold"}


def attested_policy(corpus, *, validity=None):
    raw = deepcopy(policy(corpus))
    raw["versions"] = {"vocabulary": "owner-review-vocabulary/v1", "capability": CAPABILITY_ATTESTED,
                       "fact_validity": deepcopy(validity or EXACT_INSTANT), "subject_binding": deepcopy(SUBJECT_BINDING)}
    raw["evaluator"] = {"kind": "hard_rules", "version": EVALUATOR_ATTESTED}
    return raw


# --- the contract itself ----------------------------------------------------

def test_a_v3_policy_states_both_its_temporal_and_its_subject_rule(corpus):
    parsed = parse_policy(attested_policy(corpus))
    assert type(parsed) is AttestedSubjectFactPolicy
    assert parsed.versions.subject_binding.contract == ATTESTED_CONTRACT
    assert parsed.versions.fact_validity.semantics == "exact_instant_v1"
    stated = parse_policy(attested_policy(corpus, validity=STATED_DAY))
    assert stated.versions.fact_validity.semantics == "stated_day_v1"


def test_a_v3_policy_is_not_a_v2_policy(corpus):
    from topos.permissions_v2.fact_contract import FactPolicyV2, StatedDayFactPolicy
    parsed = parse_policy(attested_policy(corpus, validity=STATED_DAY))
    assert not isinstance(parsed, StatedDayFactPolicy)
    assert isinstance(parsed, FactPolicyV2)
    # A v2 document never gains the subject rule by being relabelled.
    with pytest.raises(PolicyError):
        parse_policy(deepcopy(policy(corpus)) | {"versions": {"vocabulary": "owner-review-vocabulary/v1",
                                                              "capability": CAPABILITY_ATTESTED}})


@pytest.mark.parametrize("change", ["contract", "unattested", "rekeyed_facts", "moved", "shadowed",
                                    "missing_binding", "missing_validity", "evaluator", "extra"])
def test_every_withholding_choice_in_the_subject_block_is_the_only_choice(corpus, change):
    raw = attested_policy(corpus)
    block = raw["versions"]["subject_binding"]
    if change == "contract": block["contract"] = "legacy_single_self_v1"
    elif change == "unattested": block["unattested"] = "allow"
    elif change == "rekeyed_facts": block["rekeyed_facts"] = "allow"
    elif change == "moved": block["moved_since_attestation"] = "allow"
    elif change == "shadowed": block["literal_self_when_shadowed"] = "allow"
    elif change == "missing_binding": raw["versions"].pop("subject_binding")
    elif change == "missing_validity": raw["versions"].pop("fact_validity")
    elif change == "evaluator": raw["evaluator"]["version"] = "hard-rules/p2b-v2"
    else: block["producers"] = ["fact_store_v1"]
    with pytest.raises(PolicyError):
        parse_policy(raw)


# --- the case the first family could not serve ------------------------------

@pytest.fixture
def multi_self(timed):
    """Two self rows, and a fact whose subject is one of them."""
    with db(timed) as conn:
        add_entity(conn, "second-self")
    change_fact(timed, subject_entity_id=OWNER)
    return timed


def test_the_pre_binding_rule_refuses_this_node_on_both_counts(multi_self):
    attest(multi_self, review_id="review-multi")
    assert multi_self[0].qualify(multi_self[2], reviews=multi_self[1],
                                 contract=LEGACY_CONTRACT).reason_code == "owner_subject_ambiguous"


def test_an_unattested_entity_subject_is_withheld_under_v3(multi_self):
    attest(multi_self, review_id="review-multi")
    result = multi_self[0].qualify(multi_self[2], reviews=multi_self[1], contract=ATTESTED_CONTRACT)
    assert result.verdict == "withheld" and result.reason_code == "owner_subject_unattested"


def test_the_owners_attestation_is_what_makes_it_releasable(multi_self, projection_service):
    with db(multi_self) as conn:
        do_attest(conn, OWNER)
    attest(multi_self, review_id="review-attested")
    qualified = multi_self[0].qualify(multi_self[2], reviews=multi_self[1], contract=ATTESTED_CONTRACT)
    assert qualified.verdict == "qualified"
    assert qualified.evidence.subject_contract == ATTESTED_CONTRACT
    # The output form is unchanged: the recipient is told `self`, not an id.
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=multi_self[2]), now=1200,
                                             contract=ATTESTED_CONTRACT)
    assert preview.candidate.output.subject == "self"
    assert OWNER not in json.dumps(preview.model_dump())


def test_the_same_fact_is_still_withheld_for_an_older_grant(multi_self, projection_service):
    with db(multi_self) as conn:
        do_attest(conn, OWNER)
    attest(multi_self, review_id="review-attested")
    with owner():
        legacy = projection_service.preview(EvidenceLookup(fact_id=multi_self[2]), now=1200)
    assert legacy.candidate is None and legacy.candidate_reason_code == "owner_subject_ambiguous"


# --- signed release ---------------------------------------------------------

@pytest.fixture
def attested_release(multi_self, projection_service, tmp_path):
    """The whole v3 path, built without the pre-binding review helper.

    The shared release fixture records its output review under the legacy rule,
    which this node cannot satisfy at all. That is the point of the capability,
    so the fixture is assembled here instead of reused.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from topos.permissions_v2.fact_release import FactProjectionRelease
    from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
    from topos.permissions_v2.node_protocol import NodePolicyProtocol

    with db(multi_self) as conn:
        do_attest(conn, OWNER)
    attest(multi_self, review_id="review-attested")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=multi_self[2]), now=1200,
                                             contract=ATTESTED_CONTRACT)
        projection_service.record(RecordProjectionReview(review_id="output-review-1",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self",
                            "assertion": "explicit_atomic_preference"}), now=1200, contract=ATTESTED_CONTRACT)
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with multi_self[0]._read() as (_conn, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db",
                              identity=NodeIdentity.parse(multi_self[0].binding.model_dump()),
                              protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=multi_self[0].path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key",
        node_signing_key=node_key)
    now = [AS_OF]
    release = FactProjectionRelease(protocol=protocol, projections=projection_service, clock=lambda: now[0])
    return release, attested_policy(multi_self), cp_key, node_key, now, multi_self, None


def test_a_signed_v3_grant_releases_the_attested_subject_as_self(attested_release):
    envelope, payload = issue(attested_release)
    result, output = dispatch(attested_release, envelope, payload)[0]
    assert output == {"family": "owner_stated_fact", "operation": "read",
                      "view_id": "owner_stated_fact.scalar.v1", "subject": "self",
                      "predicate": "prefers", "value": "history books"}
    assert OWNER not in json.dumps(output) and OWNER not in json.dumps(result)


def test_the_decision_carries_the_v3_evaluator_and_nothing_identifying(attested_release):
    envelope, payload = issue(attested_release)
    dispatch(attested_release, envelope, payload)
    with sqlite3.connect(attested_release[0].protocol.ledger.path) as conn:
        rows = [row[0] for row in conn.execute("SELECT decision_json FROM p2a_receipts")]
    decisions = [AttestedSubjectFactDecision.parse(raw) for raw in rows]
    assert decisions and all(item.evaluator_version == EVALUATOR_ATTESTED for item in decisions)
    assert all(item.verdict == "permit" for item in decisions)
    assert OWNER not in json.dumps(rows)


def test_revoking_the_attestation_stops_the_next_release(attested_release, multi_self):
    from tests.permissions_v2.test_owner_identity_binding import do_revoke, entries
    envelope, payload = issue(attested_release)
    assert dispatch(attested_release, envelope, payload)
    with db(multi_self) as conn:
        do_revoke(conn, OWNER, entries(conn)[OWNER].entry_id)
    with pytest.raises(PolicyError):
        dispatch(attested_release, envelope, payload, request_id="fact-read-2",
                 send=lambda *_: pytest.fail("released after the owner withdrew the attestation"))


@pytest.mark.parametrize("model", [AttestedSubjectFactPolicy, AttestedSubjectFactDecision])
def test_attested_schema_exports_are_frozen(model):
    from pathlib import Path
    path = (Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2" / "fact_policy"
            / (model.__name__ + ".schema.json"))
    assert json.loads(path.read_text()) == model.model_json_schema()


def test_the_capability_registry_knows_exactly_three_fact_capabilities():
    from topos.permissions_v2.fact_contract import FACT_CAPABILITIES
    from topos.permissions_v2.identity import SUBJECT_CONTRACT_BY_CAPABILITY
    assert FACT_CAPABILITIES == ("permissions-beta/p2b-v1", "permissions-beta/p2b-v2",
                                 "permissions-beta/p2b-v3")
    # Every fact capability declares a subject contract, and only the newest one
    # reads attestations at all.
    assert set(FACT_CAPABILITIES) <= set(SUBJECT_CONTRACT_BY_CAPABILITY)
    attested = [key for key, value in SUBJECT_CONTRACT_BY_CAPABILITY.items() if value == ATTESTED_CONTRACT]
    assert attested == [CAPABILITY_ATTESTED]


# --- the frozen projection rule ---------------------------------------------

def test_a_legacy_grant_still_emits_only_the_literal_subject(timed, projection_service):
    """One self row, and the fact names it. The evidence layer allows it; the
    projection does not, and that is exactly the pre-binding behaviour.
    """
    change_fact(timed, subject_entity_id=OWNER)
    attest(timed, review_id="review-entity-subject")
    # The legacy permit set contains the sole self row, so eligibility passes.
    assert timed[0].qualify(timed[2], reviews=timed[1], contract=LEGACY_CONTRACT).verdict == "qualified"
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=timed[2]), now=1200)
    assert preview.candidate is None and preview.candidate_reason_code == "projection_source_restricted"
    # The attested contract is what changes it, and only after an attestation.
    with db(timed) as conn:
        do_attest(conn, OWNER)
    attest(timed, review_id="review-entity-subject-attested")
    with owner():
        attested = projection_service.preview(EvidenceLookup(fact_id=timed[2]), now=1200,
                                              contract=ATTESTED_CONTRACT)
    assert attested.candidate is not None and attested.candidate.output.subject == "self"


# --- the owner review surface passes the contract through --------------------

@pytest.mark.asyncio
async def test_the_owner_review_surface_carries_the_contract_to_the_store(timed, projection_service,
                                                                          monkeypatch):
    from types import SimpleNamespace
    from topos.core.handlers import handle_control_plane_request
    from topos.permissions_v2 import runtime as runtime_module
    from topos.principal import OWNER_APP, Principal

    change_fact(timed, subject_entity_id=OWNER)
    with db(timed) as conn:
        do_attest(conn, OWNER)
    attest(timed, review_id="review-for-handler")
    runtime = SimpleNamespace(
        protocol=SimpleNamespace(ledger=SimpleNamespace(identity=timed[0].binding)),
        projection_reviews=lambda **_: projection_service)
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: runtime)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")

    async def preview(contract=None):
        payload = {"binding": timed[0].binding.model_dump(), "request": {"fact_id": timed[2]}}
        if contract is not None:
            payload["subject_contract"] = contract
        return await handle_control_plane_request(
            {"id": "req", "type": "permissions_v2_projection_preview", "payload": payload},
            principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user=timed[0].binding.owner_id,
                                client_id="owner-ui"))

    legacy = await preview()
    assert legacy["status"] == "ok" and legacy["payload"]["candidate"] is None
    attested = await preview(ATTESTED_CONTRACT)
    assert attested["status"] == "ok" and attested["payload"]["candidate"] is not None
    assert attested["payload"]["candidate"]["output"]["subject"] == "self"
    unknown = await preview("something_else")
    assert unknown["status"] == "error" and unknown["error"] == "subject_contract_unknown"
