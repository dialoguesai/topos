"""p2b-v4 end to end: a second output family, chosen from a measurement.

The first family releases `prefers`. Nothing in the engine writes a `prefers`
fact that this contract can ever accept: the only producer of that predicate is
the LLM extractor, and it marks an owner-asserted fact `owner_only`, while this
contract requires `scoped` AND `asserted_by == "owner"`. Those two are mutually
exclusive there, so the first family has no reachable producer at all.

`works_at` does. The first-person present-tense message patterns in
`features.facts.extract` write it `scoped`, assert it as the owner, and source it
from a message table -- the only writer in the engine that does all three. That
is what "one thing the owner said about their work" means here, and it is why
this family exists rather than `works_on`, which scores higher on a corpus count
and cannot close: its only qualifying producer matches a journal CATEGORY against
a declared entity, so its lineage always terminates in `journal_entries`, which
is not a supported leaf table.

The lexical grammar, the evidence rules, the event window, correlation and the
exclusions are all v1, unchanged. Only the output family is new, and it rides
the v3 attested subject rule rather than re-deriving one.
"""
from copy import deepcopy
import json
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, owner, edit, attest, payload as change_fact
from tests.permissions_v2.test_fact_policy import AS_OF, policy, timed, utc
from tests.permissions_v2.test_fact_release import dispatch, fact_setup, issue, projection_service
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, do_attest
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence_reviews import EvidenceLookup
from topos.permissions_v2.fact_contract import (CAPABILITY_ATTESTED, CAPABILITY_WORK, EVALUATOR_WORK,
    FAMILY, FAMILY_BY_CAPABILITY, OUTPUT_FAMILIES, WORK_FAMILY, WORK_VIEW, AttestedSubjectFactPolicy,
    FactPolicyV2, FactScalarDisclosure, StatedDayFactPolicy, WorkFactDecision, WorkFactPolicy,
    WorkScalarDisclosure, atomic_label_syntax, fact_output_family)
from topos.permissions_v2.identity import ATTESTED_CONTRACT
from topos.permissions_v2.projection_reviews import RecordProjectionReview
from topos.permissions_v2.registry import parse_disclosure, parse_policy

EXACT_INSTANT = {"semantics": "exact_instant_v1", "precision": "instant", "instants": "explicit_utc_exact",
                 "unknown": "withhold"}
STATED_DAY = {"semantics": "stated_day_v1", "precision": "day", "timezone_basis": "unrecorded_any_earth_offset",
              "current_from": "next_day_12_00_utc", "instants": "explicit_utc_exact", "unknown": "withhold",
              "not_elapsed": "withhold"}
SUBJECT_BINDING = {"contract": "owner_attested_v1", "statement_version": "owner-identity-attestation/v1",
                   "subjects": "owner_attested_entities", "unattested": "withhold",
                   "moved_since_attestation": "withhold", "rekeyed_facts": "withhold",
                   "literal_self_when_shadowed": "withhold"}
OUTPUT_FAMILY = {"name": "owner_stated_work", "view_id": "owner_stated_work.scalar.v1",
                 "predicate": "works_at", "assertion": "explicit_atomic_work_engagement",
                 "producer": "first_person_present_tense_message_statement_v1",
                 "projection_version": "exact-owner-work/v1", "other_predicates": "withhold"}
EMPLOYER = "Ferrograph Instruments"


def work_policy(corpus, *, validity=None, family=None):
    raw = deepcopy(policy(corpus))
    raw["versions"] = {"vocabulary": "owner-review-vocabulary/v1", "capability": CAPABILITY_WORK,
                       "fact_validity": deepcopy(validity or EXACT_INSTANT),
                       "subject_binding": deepcopy(SUBJECT_BINDING),
                       "output_family": deepcopy(family or OUTPUT_FAMILY)}
    raw["evaluator"] = {"kind": "hard_rules", "version": EVALUATOR_WORK}
    for rule in raw["rules"]:
        rule["release"]["forms"] = [{"family": WORK_FAMILY, "operation": "read", "view_id": WORK_VIEW}]
    return raw


@pytest.fixture
def work_fact(timed):
    """The corpus fact, restated as the thing the owner said about their work."""
    change_fact(timed, predicate="works_at", object_value=EMPLOYER, subject_entity_id=OWNER)
    return timed


# --- the contract itself ----------------------------------------------------

def test_a_v4_policy_states_its_family_its_subject_rule_and_its_clock(corpus):
    parsed = parse_policy(work_policy(corpus))
    assert type(parsed) is WorkFactPolicy
    assert fact_output_family(parsed) == WORK_FAMILY
    assert parsed.versions.subject_binding.contract == ATTESTED_CONTRACT
    assert parsed.versions.fact_validity.semantics == "exact_instant_v1"
    # The family is compositional with the clock, exactly as v3 is.
    stated = parse_policy(work_policy(corpus, validity=STATED_DAY))
    assert stated.versions.fact_validity.semantics == "stated_day_v1"
    assert fact_output_family(stated) == WORK_FAMILY


def test_a_v4_policy_is_not_a_v3_or_v2_policy(corpus):
    parsed = parse_policy(work_policy(corpus))
    assert not isinstance(parsed, (AttestedSubjectFactPolicy, StatedDayFactPolicy))
    assert isinstance(parsed, FactPolicyV2)
    # Relabelling a v3 document does not give it a family.
    from tests.permissions_v2.test_fact_attested_subject import attested_policy
    with pytest.raises(PolicyError):
        parse_policy(attested_policy(corpus) | {"versions": {"vocabulary": "owner-review-vocabulary/v1",
            "capability": CAPABILITY_WORK, "fact_validity": deepcopy(EXACT_INSTANT),
            "subject_binding": deepcopy(SUBJECT_BINDING)}})


def test_every_pre_v4_capability_still_means_the_first_family():
    assert FAMILY_BY_CAPABILITY == {"permissions-beta/p2b-v1": FAMILY, "permissions-beta/p2b-v2": FAMILY,
                                    "permissions-beta/p2b-v3": FAMILY, "permissions-beta/p2b-v4": WORK_FAMILY}


@pytest.mark.parametrize("change", ["name", "view", "predicate", "assertion", "producer",
                                    "projection_version", "other_predicates", "missing", "extra"])
def test_every_premise_in_the_family_block_is_pinned(corpus, change):
    """The family block is a recorded premise, so none of it may be re-chosen.

    `producer` is the one that matters most: it names the writer whose semantics
    this family claims. It is not enforceable by the contract -- nothing here can
    inspect the engine -- but pinning it means a signed v4 policy stops parsing
    the day someone decides the family should mean something else.
    """
    raw = work_policy(corpus)
    block = raw["versions"]["output_family"]
    if change == "name": block["name"] = FAMILY
    elif change == "view": block["view_id"] = "owner_stated_fact.scalar.v1"
    elif change == "predicate": block["predicate"] = "works_on"
    elif change == "assertion": block["assertion"] = "explicit_atomic_preference"
    elif change == "producer": block["producer"] = "journal_category_entity_match_v1"
    elif change == "projection_version": block["projection_version"] = "exact-owner-preference/v1"
    elif change == "other_predicates": block["other_predicates"] = "allow"
    elif change == "missing": raw["versions"].pop("output_family")
    else: block["tables"] = ["journal_entries"]
    with pytest.raises(PolicyError):
        parse_policy(raw)


def test_the_family_strings_are_written_down_once_and_agree_everywhere():
    """A family-defining string appears in a module constant AND in model Literals.

    A `Literal[...]` cannot take a variable, so the spellings are necessarily
    duplicated. What must not happen is that they drift apart silently, which is
    why this asserts the constants against the Literals actually compiled into
    the models rather than against themselves. Same shape as the check that keeps
    the three copies of ATTESTATION_STATEMENT in agreement.
    """
    from typing import get_args
    from topos.permissions_v2.fact_contract import (PROJECTION_VERSION, VIEW, WORK_ASSERTION,
        WORK_PREDICATE, WORK_PROJECTION_VERSION, OwnerStatedWorkFamily)
    from topos.permissions_v2.fact_projection import (FactProjectionCandidate, OutputClassification,
        WorkFactProjectionCandidate, WorkOutputClassification)

    def only(model, field):
        args = get_args(model.model_fields[field].annotation)
        assert len(args) == 1, f"{model.__name__}.{field} is no longer a single literal"
        return args[0]

    assert WORK_PREDICATE == only(WorkScalarDisclosure, "predicate") == only(OwnerStatedWorkFamily, "predicate")
    assert WORK_ASSERTION == only(WorkOutputClassification, "assertion") == only(OwnerStatedWorkFamily, "assertion")
    assert WORK_FAMILY == only(WorkScalarDisclosure, "family") == only(OwnerStatedWorkFamily, "name")
    assert WORK_VIEW == only(WorkScalarDisclosure, "view_id") == only(OwnerStatedWorkFamily, "view_id")
    assert (WORK_PROJECTION_VERSION == only(WorkFactProjectionCandidate, "projection_version")
            == only(OwnerStatedWorkFamily, "projection_version"))
    # And the first family, so this test also guards what must not move.
    assert FAMILY == only(FactScalarDisclosure, "family")
    assert VIEW == only(FactScalarDisclosure, "view_id")
    assert PROJECTION_VERSION == only(FactProjectionCandidate, "projection_version")
    assert only(FactScalarDisclosure, "predicate") == "prefers"
    assert only(OutputClassification, "assertion") == "explicit_atomic_preference"


@pytest.mark.parametrize("capability", ["permissions-beta/p2b-v1", "permissions-beta/p2b-v2",
                                        "permissions-beta/p2b-v3", "permissions-beta/p2b-v4"])
def test_a_policy_can_never_declare_one_family_and_release_another(corpus, capability):
    """The rule's release form and the capability's family cannot disagree.

    Both are closed Literals on classes selected by the same capability, so this
    holds by construction rather than by a runtime check -- and this test is what
    says so. If a later change makes either side a union, the invariant stops
    being structural and a real cross-family release path opens.
    """
    from tests.permissions_v2.test_fact_attested_subject import attested_policy
    from tests.permissions_v2.test_fact_stated_day import stated_day_policy
    builder = {"permissions-beta/p2b-v1": lambda c: policy(c),
               "permissions-beta/p2b-v2": stated_day_policy,
               "permissions-beta/p2b-v3": attested_policy,
               "permissions-beta/p2b-v4": work_policy}[capability]
    parsed = parse_policy(builder(corpus))
    expected = FAMILY_BY_CAPABILITY[capability]
    assert fact_output_family(parsed) == expected
    for rule in parsed.rules:
        for form in rule.release.forms:
            assert form.family == expected
            assert form.view_id == OUTPUT_FAMILIES[expected][0]


def test_a_v4_rule_may_only_release_the_work_form(corpus):
    raw = work_policy(corpus)
    raw["rules"][0]["release"]["forms"] = [{"family": FAMILY, "operation": "read",
                                            "view_id": "owner_stated_fact.scalar.v1"}]
    with pytest.raises(PolicyError):
        parse_policy(raw)


# --- the two families never stand in for each other -------------------------

def test_the_work_disclosure_carries_only_works_at():
    good = {"family": WORK_FAMILY, "operation": "read", "view_id": WORK_VIEW, "subject": "self",
            "predicate": "works_at", "value": EMPLOYER}
    assert WorkScalarDisclosure.parse(good).value == EMPLOYER
    # `works_on` is the predicate with the higher corpus count and no reachable
    # producer. It is deliberately not in this family.
    with pytest.raises(PolicyError):
        WorkScalarDisclosure.parse(good | {"predicate": "works_on"})
    with pytest.raises(PolicyError):
        WorkScalarDisclosure.parse(good | {"predicate": "prefers"})
    with pytest.raises(PolicyError):
        FactScalarDisclosure.parse(good | {"family": FAMILY, "view_id": "owner_stated_fact.scalar.v1"})


def test_the_registry_hands_each_capability_its_own_disclosure():
    work = {"family": WORK_FAMILY, "operation": "read", "view_id": WORK_VIEW, "subject": "self",
            "predicate": "works_at", "value": EMPLOYER}
    preference = {"family": FAMILY, "operation": "read", "view_id": "owner_stated_fact.scalar.v1",
                  "subject": "self", "predicate": "prefers", "value": "history books"}
    assert type(parse_disclosure(work, capability=CAPABILITY_WORK)) is WorkScalarDisclosure
    assert type(parse_disclosure(preference, capability=CAPABILITY_ATTESTED)) is FactScalarDisclosure
    # Crossed, both directions.
    with pytest.raises(PolicyError): parse_disclosure(work, capability=CAPABILITY_ATTESTED)
    with pytest.raises(PolicyError): parse_disclosure(preference, capability=CAPABILITY_WORK)


@pytest.mark.parametrize("value,accepted", [
    (EMPLOYER, True), ("Acme & Co", True), ("history books", True), ("Studio 54", True),
    ("Acme  Labs", False), ("Acme_Labs", False), ("'Acme", False), ("Acme.", False),
    ("", False), ("   ", False), ("&&&", False), ("Acme\nLabs", False)])
def test_both_families_share_one_label_grammar(value, accepted):
    """The grammar was named, not widened, when the second family arrived.

    Naming it is the only change to the first family in this capability, so this
    battery is the proof that it still accepts and rejects exactly what it did.
    An employer is not a looser label than a preference: `Acme.` and `Acme_Labs`
    are both refused, and the producer never emits them anyway -- `_clean_object`
    splits on the period, so a trailing one cannot reach here.
    """
    for model, extra in ((FactScalarDisclosure, {"family": FAMILY, "predicate": "prefers",
                                                 "view_id": "owner_stated_fact.scalar.v1"}),
                         (WorkScalarDisclosure, {"family": WORK_FAMILY, "predicate": "works_at",
                                                 "view_id": WORK_VIEW})):
        raw = {"operation": "read", "subject": "self", "value": value, **extra}
        if accepted:
            assert model.parse(raw).value == value
        else:
            with pytest.raises(PolicyError):
                model.parse(raw)
    if accepted:
        assert atomic_label_syntax(value) == value
    else:
        with pytest.raises((ValueError, TypeError)):
            atomic_label_syntax(value)


# --- the owner review is per family -----------------------------------------

def test_the_preview_under_a_work_grant_is_the_work_candidate(work_fact, projection_service):
    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    assert preview.candidate.output.family == WORK_FAMILY
    assert preview.candidate.output.predicate == "works_at"
    assert preview.candidate.output.subject == "self"
    assert preview.candidate.projection_version == "exact-owner-work/v1"
    # Still no entity id anywhere in what the owner is shown.
    assert OWNER not in json.dumps(preview.model_dump())


def test_a_works_at_fact_is_not_previewable_under_the_first_family(work_fact, projection_service):
    """The predicate decides nothing; the grant does. Asking for this fact under
    the preference family fails at the disclosure schema rather than relabelling.
    """
    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT)
    assert preview.candidate is None and preview.candidate_reason_code == "schema_invalid"


def test_a_prefers_fact_is_not_previewable_under_the_work_family(timed, projection_service):
    change_fact(timed, subject_entity_id=OWNER)
    with db(timed) as conn:
        do_attest(conn, OWNER)
    attest(timed, review_id="review-preference")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=timed[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    assert preview.candidate is None and preview.candidate_reason_code == "schema_invalid"


def test_a_review_recorded_for_one_family_is_not_served_to_the_other(work_fact, projection_service):
    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
        projection_service.record(RecordProjectionReview(review_id="output-review-work",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self",
                            "assertion": "explicit_atomic_work_engagement"}),
            now=1200, contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
        # The stored review is a work review. Read back under the first family it
        # does not parse, and that is a store binding failure rather than a
        # withheld candidate: `_current_in` sits outside the withhold handler on
        # purpose, so a review row that is not the family asked for refuses
        # loudly instead of looking like "the owner has not reviewed this yet".
        with pytest.raises(PolicyError):
            projection_service.read(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                    contract=ATTESTED_CONTRACT)
        # Under its own family it is current, so the refusal is about the family
        # and not about the review having been damaged.
        own = projection_service.read(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                      contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    assert own.qualification.verdict == "reviewed"


def test_a_work_candidate_cannot_be_reviewed_with_a_preference_assertion(work_fact, projection_service):
    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
        # Through .parse, the way the service receives it: a crossed review is a
        # schema refusal at the door, before any store or candidate is touched.
        with pytest.raises(PolicyError):
            RecordProjectionReview.parse({"review_id": "output-review-crossed",
                "expected_candidate": preview.candidate.model_dump(),
                "expected_candidate_hash": preview.candidate_hash,
                "expected_current_review_revision": None,
                "classification": {"domains": ["reading"], "sensitivity": "personal", "subject": "self",
                                   "assertion": "explicit_atomic_preference"}})


def test_eligibility_names_the_family_mismatch_rather_than_a_schema_error(work_fact, projection_service):
    """A reviewed projection from one family, evaluated under the other policy.

    Both refusals are safe -- the disclosure literals would refuse it anyway at
    parse -- but they are not equally useful. The exact-type check runs first so
    the reason is `fact_policy_projection_stale`, which says a reviewed
    projection was paired with a policy it was not reviewed for, rather than
    `schema_invalid`, which reads like a corrupt document. Remove the check and
    this test fails on the reason code, which is the point of having it.
    """
    from topos.permissions_v2.fact_policy import fact_projection_decision
    from tests.permissions_v2.test_fact_attested_subject import attested_policy
    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
        projection_service.record(RecordProjectionReview(review_id="output-review-work",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self",
                            "assertion": "explicit_atomic_work_engagement"}),
            now=1200, contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    wrong = parse_policy(attested_policy(work_fact))
    captured = {}

    def evaluate(evidence, reviewed, rows, permits):
        try:
            fact_projection_decision(policy=wrong, evidence=evidence, projection=reviewed, rows=rows,
                                     binding=wrong.binding, request_as_of=AS_OF, now=AS_OF,
                                     permitted_subjects=permits)
        except PolicyError as exc:
            captured["code"] = exc.code

    projection_service.with_reviewed(work_fact[2], now=1200, callback=evaluate,
                                     contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    assert captured["code"] == "fact_policy_projection_stale"


# --- signed release ---------------------------------------------------------

@pytest.fixture
def work_release(work_fact, projection_service, tmp_path):
    """The whole v4 path. Assembled here for the same reason v3's fixture is:
    the shared helper records its review under the first family and the legacy
    subject rule, and this node satisfies neither."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from topos.permissions_v2.fact_release import FactProjectionRelease
    from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
    from topos.permissions_v2.node_protocol import NodePolicyProtocol

    with db(work_fact) as conn:
        do_attest(conn, OWNER)
    attest(work_fact, review_id="review-work")
    with owner():
        preview = projection_service.preview(EvidenceLookup(fact_id=work_fact[2]), now=1200,
                                             contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
        projection_service.record(RecordProjectionReview(review_id="output-review-work",
            expected_candidate=preview.candidate, expected_candidate_hash=preview.candidate_hash,
            expected_current_review_revision=None,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self",
                            "assertion": "explicit_atomic_work_engagement"}),
            now=1200, contract=ATTESTED_CONTRACT, family=WORK_FAMILY)
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with work_fact[0]._read() as (_conn, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db",
                              identity=NodeIdentity.parse(work_fact[0].binding.model_dump()),
                              protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=work_fact[0].path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key",
        node_signing_key=node_key)
    now = [AS_OF]
    release = FactProjectionRelease(protocol=protocol, projections=projection_service, clock=lambda: now[0])
    return release, work_policy(work_fact), cp_key, node_key, now, work_fact, None


def test_a_signed_v4_grant_releases_the_work_scalar(work_release):
    envelope, payload = issue(work_release)
    result, output = dispatch(work_release, envelope, payload)[0]
    assert output == {"family": "owner_stated_work", "operation": "read",
                      "view_id": "owner_stated_work.scalar.v1", "subject": "self",
                      "predicate": "works_at", "value": EMPLOYER}
    assert OWNER not in json.dumps(output) and OWNER not in json.dumps(result)


def test_the_decision_carries_the_v4_evaluator_and_the_v4_view(work_release):
    envelope, payload = issue(work_release)
    dispatch(work_release, envelope, payload)
    with sqlite3.connect(work_release[0].protocol.ledger.path) as conn:
        rows = [row[0] for row in conn.execute("SELECT decision_json FROM p2a_receipts")]
    decisions = [WorkFactDecision.parse(raw) for raw in rows]
    assert decisions and all(item.evaluator_version == EVALUATOR_WORK for item in decisions)
    assert all(item.verdict == "permit" for item in decisions)
    assert all(item.required_projection_id == WORK_VIEW for item in decisions)
    assert OWNER not in json.dumps(rows)


def test_revoking_the_attestation_stops_the_next_work_release(work_release, work_fact):
    from tests.permissions_v2.test_owner_identity_binding import do_revoke, entries
    envelope, payload = issue(work_release)
    assert dispatch(work_release, envelope, payload)
    with db(work_fact) as conn:
        do_revoke(conn, OWNER, entries(conn)[OWNER].entry_id)
    with pytest.raises(PolicyError):
        dispatch(work_release, envelope, payload, request_id="fact-read-2",
                 send=lambda *_: pytest.fail("released after the owner withdrew the attestation"))


# --- frozen exports ---------------------------------------------------------

@pytest.mark.parametrize("model", [WorkFactPolicy, WorkFactDecision, WorkScalarDisclosure])
def test_work_schema_exports_are_frozen(model):
    from pathlib import Path
    path = (Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2" / "fact_policy"
            / (model.__name__ + ".schema.json"))
    assert json.loads(path.read_text()) == model.model_json_schema()


def test_the_first_familys_exports_did_not_move():
    """The point of the whole capability: nothing about the shipped family changed.

    Compared against the checked-in fixtures rather than against itself, so this
    fails if the v4 work reshaped a v1/v2/v3 schema by accident.
    """
    from pathlib import Path
    from topos.permissions_v2.fact_contract import (AttestedSubjectFactDecision, FactDecision,
        StatedDayFactDecision, StatedDayFactPolicy)
    base = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2" / "fact_policy"
    for model in (FactPolicyV2, FactDecision, FactScalarDisclosure, StatedDayFactPolicy,
                  StatedDayFactDecision, AttestedSubjectFactPolicy, AttestedSubjectFactDecision):
        assert json.loads((base / (model.__name__ + ".schema.json")).read_text()) == model.model_json_schema()
    assert OUTPUT_FAMILIES[FAMILY] == ("owner_stated_fact.scalar.v1", "exact-owner-preference/v1",
                                       FactScalarDisclosure)
