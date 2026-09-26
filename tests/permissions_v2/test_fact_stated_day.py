"""Explicit stated-day (P2b v2) fact validity; the v1 exact-instant class stays frozen.

The second capability changes only which `valid_from` values count as current.
Reviews, lineage, the contributor event window, deny precedence and the exact
output shape are the P2b v1 rules. No copied corpus, model or recipient client
is involved; the golden vector is a synthetic frozen wire sample.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_evidence import corpus, owner, edit, attest
from tests.permissions_v2.test_fact_policy import timed, policy, bundle, AS_OF, utc
from tests.permissions_v2 import test_fact_eligibility as eligibility
from tests.permissions_v2.test_fact_release import issue, dispatch
from tests.permissions_v2.test_fact_bridge import bridge as shadow_bridge, record_output, Fake
from tests.permissions_v2.test_projection_reviews import service as projection_service, prepare
from tests.permissions_v2.test_release import recipient
from topos.features.facts.store import FactStore
from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest
from topos.permissions_v2.contract import Binding, PolicyV2
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore, _key
from topos.permissions_v2.fact_contract import (CAPABILITY, CAPABILITY_STATED_DAY, EVALUATOR_STATED_DAY,
    FACT_VALIDITY_EXACT_INSTANT, FACT_VALIDITY_STATED_DAY, FactDecision, FactPolicyV2, FactScalarDisclosure,
    StatedDayFactDecision, StatedDayFactPolicy, fact_validity_semantics)
from topos.permissions_v2.fact_eligibility import (canonical_utc_microseconds, fact_current_from_microseconds,
    prepare_fact_eligibility, stated_day_elapsed_microseconds)
from topos.permissions_v2.fact_policy import fact_projection_decision
from topos.permissions_v2.fact_release import FactProjectionRelease
from topos.permissions_v2.forwarding import SignedNodeResult, node_result_signing_bytes, verify_node_result
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.projection_reviews import ProjectionReviewService, ProjectionReviewStore
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.permissions_v2.protocol import MutationBody, SignedMutation, command_digest, protocol_signing_bytes, sign_mutation, verify_ack
from topos.permissions_v2.registry import parse_decision, parse_disclosure, parse_policy
from topos.permissions_v2.signing import (AuthorityBinding, FactAuthorityBinding, FactEnvelopeBody, FactRequestContext,
    SignedFactEnvelope, parse_authority, parse_envelope, request_digest, sign_envelope, signing_bytes, verify_envelope)
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up

STATED_DAY = {"semantics": "stated_day_v1", "precision": "day", "timezone_basis": "unrecorded_any_earth_offset",
    "current_from": "next_day_12_00_utc", "instants": "explicit_utc_exact", "unknown": "withhold", "not_elapsed": "withhold"}
# AS_OF is 2027-01-15T08:00:00Z. A day stated as 2027-01-13 has ended at every Earth
# offset by 2027-01-14T12:00:00Z; a day stated as 2027-01-14 only at 2027-01-15T12:00:00Z.
ELAPSED, PENDING = "2027-01-13", "2027-01-14"
PENDING_ELAPSES_AT = AS_OF + 4 * 3600
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2" / "fact_policy" / "signed-golden-v2.json"
OUTPUT = {"family": "owner_stated_fact", "operation": "read", "view_id": "owner_stated_fact.scalar.v1",
    "subject": "self", "predicate": "prefers", "value": "history books"}


def stated_day_policy(corpus, *, validity_hours=1):
    raw = policy(corpus)
    raw["versions"] = {"vocabulary": "owner-review-vocabulary/v1", "capability": CAPABILITY_STATED_DAY, "fact_validity": deepcopy(STATED_DAY)}
    raw["evaluator"] = {"kind": "hard_rules", "version": EVALUATOR_STATED_DAY}
    raw["validity"] = {"starts_at": AS_OF - 3600, "expires_at": AS_OF + validity_hours * 3600}
    return raw


def to_v1(raw):
    raw["versions"] = {"vocabulary": "owner-review-vocabulary/v1", "capability": CAPABILITY}
    raw["evaluator"] = {"kind": "hard_rules", "version": "hard-rules/p2b-v1"}
    return raw


def decide(corpus, raw, **options):
    supplied = options.pop("supplied", None) or bundle(corpus)
    return fact_projection_decision(policy=parse_policy(raw), **supplied, binding=Binding.parse(raw["binding"]),
        request_as_of=options.pop("request_as_of", AS_OF), now=options.pop("now", AS_OF))


def set_valid_from(corpus, value):
    edit(corpus, "UPDATE signal_objects SET valid_from=?", (value,))


def test_stated_day_elapses_at_noon_utc_on_the_following_day():
    assert stated_day_elapsed_microseconds(ELAPSED) == int(datetime(2027, 1, 14, 12, tzinfo=timezone.utc).timestamp()) * 1_000_000
    assert stated_day_elapsed_microseconds(PENDING) == PENDING_ELAPSES_AT * 1_000_000
    assert stated_day_elapsed_microseconds("2024-02-29") is not None and stated_day_elapsed_microseconds("2023-02-29") is None
    for value in [None, "", 0, True, "2027", "2027-01", "2027-1-5", "2027-02-30", "2027-13-01", "2027-01-13T08:00:00Z",
                  "2027-01-13T08:00:00", " 2027-01-13", "2027-01-13\n", "20270113", "2027-01-13+01:00", "٢٠٢٧-01-13"]:
        assert stated_day_elapsed_microseconds(value) is None
    assert fact_current_from_microseconds(ELAPSED, semantics=FACT_VALIDITY_EXACT_INSTANT) is None
    assert fact_current_from_microseconds(ELAPSED, semantics=FACT_VALIDITY_STATED_DAY) == stated_day_elapsed_microseconds(ELAPSED)
    instant = utc(AS_OF, 7)
    assert fact_current_from_microseconds(instant, semantics=FACT_VALIDITY_STATED_DAY) == canonical_utc_microseconds(instant)


@pytest.mark.parametrize("value", [ELAPSED, PENDING, "2027", "2027-01"])
def test_v1_policy_never_accepts_stated_periods(timed, value):
    set_valid_from(timed, value)
    result = decide(timed, policy(timed))
    assert type(result) is FactDecision and result.evaluator_version == "hard-rules/p2b-v1"
    assert result.verdict == "indeterminate" and result.missing_context_codes == ["fact_validity"]


def test_elapsed_stated_day_is_current_only_under_the_v2_capability(timed):
    set_valid_from(timed, ELAPSED)
    supplied = bundle(timed)
    v1 = decide(timed, policy(timed), supplied=supplied)
    v2 = decide(timed, stated_day_policy(timed), supplied=supplied)
    assert v1.verdict == "indeterminate" and v1.missing_context_codes == ["fact_validity"]
    assert type(v2) is StatedDayFactDecision and v2.evaluator_version == EVALUATOR_STATED_DAY
    assert (v2.verdict, v2.reason_code, v2.matched_allow_clause_ids) == ("permit", "rule_permit", ["allow-reading"])
    assert v2.required_projection_id == "owner_stated_fact.scalar.v1" and v2.missing_context_codes == []
    assert v2.candidate_revision == v1.candidate_revision and v2.policy_hash != v1.policy_hash


@pytest.mark.parametrize("anchor,expected", [(AS_OF, "deny"), (PENDING_ELAPSES_AT - 1, "deny"), (PENDING_ELAPSES_AT, "permit")])
def test_pending_day_boundary_is_exact_at_noon_utc(timed, anchor, expected):
    set_valid_from(timed, PENDING)
    result = decide(timed, stated_day_policy(timed, validity_hours=6), request_as_of=anchor, now=anchor)
    assert result.verdict == expected
    assert result.reason_code == ("rule_permit" if expected == "permit" else "fact_not_current")


@pytest.mark.parametrize("value", ["2027-01-15", "2027-02-01", "2099-12-31"])
def test_future_or_unelapsed_stated_day_is_not_current_rather_than_unknown(timed, value):
    set_valid_from(timed, value)
    result = decide(timed, stated_day_policy(timed))
    assert (result.verdict, result.reason_code, result.missing_context_codes) == ("deny", "fact_not_current", [])


@pytest.mark.parametrize("value", ["2027", "2027-01", "2027-1-5", "2027-02-30", "2027-01-13T08:00:00",
    "2027-01-13T09:00:00+01:00", " 2027-01-13", "2027-01-13\n", "20270113", "", "unparseable"])
def test_other_precisions_stay_unknown_under_v2(timed, value):
    set_valid_from(timed, value)
    result = decide(timed, stated_day_policy(timed))
    assert result.verdict == "indeterminate" and result.missing_context_codes == ["fact_validity"]


@pytest.mark.parametrize("value,expected", [(utc(AS_OF), "permit"), (utc(AS_OF, 1), "deny"),
    (utc(AS_OF - 86400 * 400), "permit"), (utc(AS_OF - 1).replace("Z", "+00:00"), "permit")])
def test_exact_instants_keep_their_frozen_v1_meaning_under_v2(timed, value, expected):
    set_valid_from(timed, value)
    supplied = bundle(timed)
    raw = policy(timed)
    frozen = eligibility.outcome(eligibility.oracle.fact_projection_decision, dict(policy=FactPolicyV2.parse(raw),
        **deepcopy(supplied), binding=Binding.parse(raw["binding"]), request_as_of=AS_OF, now=AS_OF))
    current = decide(timed, stated_day_policy(timed), supplied=supplied)
    assert current.verdict == expected
    assert frozen[0] == "decision"
    expected_fields = json.loads(frozen[1])
    actual_fields = current.model_dump()
    assert actual_fields.pop("evaluator_version") == EVALUATOR_STATED_DAY and expected_fields.pop("evaluator_version") == "hard-rules/p2b-v1"
    assert actual_fields.pop("policy_hash") != expected_fields.pop("policy_hash")
    assert actual_fields == expected_fields


@pytest.mark.parametrize("event,verdict,reason", [(utc(AS_OF - 90000), "deny", "rule_deny"), (None, "indeterminate", "unknown_context")])
def test_stated_day_never_rescues_old_or_unknown_leaf_events(timed, event, verdict, reason):
    set_valid_from(timed, ELAPSED)
    edit(timed, "UPDATE conversation_messages SET event_at=?", (event,))
    result = decide(timed, stated_day_policy(timed))
    assert (result.verdict, result.reason_code, result.matched_allow_clause_ids) == (verdict, reason, [])
    assert result.missing_context_codes == (["time"] if event is None else [])


def test_temporal_restamp_stales_reviews_and_cannot_reuse_the_old_snapshot(timed):
    supplied = bundle(timed)
    assert decide(timed, stated_day_policy(timed), supplied=supplied).verdict == "permit"
    set_valid_from(timed, ELAPSED)
    assert timed[0].qualify(timed[2], reviews=timed[1]).verdict == "withheld"
    root = next(ref for ref in supplied["evidence"].snapshot.artifacts if ref.identity.record_id == timed[2])
    supplied["rows"][_key(root.identity)]["valid_from"] = ELAPSED
    with pytest.raises(PolicyError, match="fact_policy_revision"):
        decide(timed, stated_day_policy(timed), supplied=supplied)


def test_owner_correction_closes_the_stated_day_row_and_the_replacement_is_reviewed_afresh(timed):
    from topos.features.facts.verdicts import edit_fact
    set_valid_from(timed, ELAPSED)
    raw = stated_day_policy(timed)
    supplied = bundle(timed)
    assert decide(timed, raw, supplied=supplied).verdict == "permit"
    # 'prefers' is multi-valued, so FactStore alone never supersedes it; the owner
    # edit verdict is the path that closes the old value.
    with sqlite3.connect(timed[0].path) as conn:
        corrected = edit_fact(conn, timed[2], object_value="science books", note="synthetic correction")
        rows = {row[0]: row for row in conn.execute("SELECT object_id, valid_from, valid_to FROM signal_objects WHERE object_type='fact'")}
    replacement = corrected["object_id"]
    assert corrected["superseded_object_id"] == timed[2] and replacement != timed[2] and set(rows) == {timed[2], replacement}
    assert rows[timed[2]][1] == ELAPSED and rows[timed[2]][2] is not None
    assert rows[replacement][2] is None
    assert timed[0].qualify(timed[2], reviews=timed[1]).reason_code == "evidence_deleted"
    root = next(ref for ref in supplied["evidence"].snapshot.artifacts if ref.identity.record_id == timed[2])
    supplied["rows"][_key(root.identity)]["valid_to"] = rows[timed[2]][2]
    with pytest.raises(PolicyError, match="fact_policy_revision"):
        decide(timed, raw, supplied=supplied)
    assert timed[0].qualify(replacement, reviews=timed[1]).reason_code == "implicit_review_current_evidence"
    attest((timed[0], timed[1], replacement), review_id="review-replacement")
    assert timed[0].qualify(replacement, reviews=timed[1]).reason_code == "owner_reviewed_current_evidence"
    assert timed[0].qualify(timed[2], reviews=timed[1]).reason_code == "evidence_deleted"


@pytest.mark.parametrize("change", ["missing_block", "semantics", "precision", "timezone_basis", "current_from", "instants",
    "unknown", "not_elapsed", "extra_field", "evaluator_v1", "capability_v1_with_block", "vocabulary", "capability_v3"])
def test_stated_day_policy_is_closed_and_explicit(timed, change):
    raw = stated_day_policy(timed)
    block = raw["versions"]["fact_validity"]
    if change == "missing_block": del raw["versions"]["fact_validity"]
    elif change in STATED_DAY:
        block[change] = {"semantics": "exact_instant_v1", "precision": "year", "timezone_basis": "utc", "current_from": "start_of_day_utc",
            "instants": "any", "unknown": "allow", "not_elapsed": "allow"}[change]
    elif change == "extra_field": block["producers"] = ["fact_store_v1"]
    elif change == "evaluator_v1": raw["evaluator"]["version"] = "hard-rules/p2b-v1"
    elif change == "capability_v1_with_block": raw["versions"]["capability"] = CAPABILITY
    elif change == "capability_v3": raw["versions"]["capability"] = "permissions-beta/p2b-v3"
    else: raw["versions"]["vocabulary"] = "owner-review-vocabulary/v2"
    with pytest.raises(PolicyError): parse_policy(raw)
    with pytest.raises(PolicyError): StatedDayFactPolicy.parse(raw)
    with pytest.raises(PolicyError): FactPolicyV2.parse(raw)


def test_registry_keeps_v1_frozen_and_dispatches_v2_separately(timed):
    v1, v2 = parse_policy(policy(timed)), parse_policy(stated_day_policy(timed))
    assert type(v1) is FactPolicyV2 and type(v2) is StatedDayFactPolicy and isinstance(v2, FactPolicyV2)
    assert fact_validity_semantics(v1) == FACT_VALIDITY_EXACT_INSTANT and fact_validity_semantics(v2) == FACT_VALIDITY_STATED_DAY
    assert set(v1.model_dump()["versions"]) == {"vocabulary", "capability"}
    assert "fact_validity" not in json.dumps(FactPolicyV2.model_json_schema())
    assert "stated_day_v1" in json.dumps(StatedDayFactPolicy.model_json_schema())
    for model in (FactPolicyV2, PolicyV2):
        with pytest.raises(PolicyError): model.parse(stated_day_policy(timed))
    with pytest.raises(PolicyError): StatedDayFactPolicy.parse(policy(timed))
    supplied = bundle(timed)
    decision = decide(timed, stated_day_policy(timed), supplied=supplied)
    assert parse_decision(decision.model_dump(), capability=CAPABILITY_STATED_DAY) == decision
    with pytest.raises(PolicyError): parse_decision(decision.model_dump(), capability=CAPABILITY)
    with pytest.raises(PolicyError): parse_decision(decide(timed, policy(timed), supplied=supplied).model_dump(), capability=CAPABILITY_STATED_DAY)
    for capability in (CAPABILITY, CAPABILITY_STATED_DAY):
        assert parse_disclosure(OUTPUT, capability=capability) == FactScalarDisclosure.parse(OUTPUT)
    raw = stated_day_policy(timed)
    for unparsed in (raw, PolicyV2.parse(__import__("tests.permissions_v2.test_contract_and_ledger", fromlist=["sample_policy"]).sample_policy())):
        with pytest.raises(PolicyError, match="fact_policy_binding"):
            prepare_fact_eligibility(policy=unparsed, **supplied, binding=Binding.parse(raw["binding"]), request_as_of=AS_OF, now=AS_OF)


def test_signed_authority_and_envelope_accept_only_fact_capabilities(timed):
    raw = stated_day_policy(timed)
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1, "policy_version_id": raw["policy_version_id"],
        "policy_hash": digest(parse_policy(raw).model_dump()), "capability_version": CAPABILITY_STATED_DAY,
        "protection_revision": "a" * 64, "node_epoch": 1}
    parsed = parse_authority(authority)
    assert type(parsed) is FactAuthorityBinding and parsed.capability_version == CAPABILITY_STATED_DAY
    with pytest.raises(PolicyError): AuthorityBinding.parse(authority)
    # Not the next version number: that became a real capability in p2b-v4 and
    # quietly turned this line green-for-the-wrong-reason. A sentinel that can
    # never be minted keeps the assertion about the closed set, not about arithmetic.
    with pytest.raises(PolicyError): parse_authority(authority | {"capability_version": "permissions-beta/p2b-vnext"})
    body = {**authority, "version": "topos-grantee-envelope/v2", "kid": "cp-key", "request_id": "request-1",
        "request_type": "permissions.v2.fact.read", "request_hash": "b" * 64, "issued_at": AS_OF, "expires_at": AS_OF + 100}
    assert type(parse_envelope(body, signed=False)) is FactEnvelopeBody
    with pytest.raises(PolicyError): parse_envelope(body | {"request_type": "permissions.v2.read"}, signed=False)


@pytest.fixture
def stated_setup(timed, projection_service, tmp_path):
    set_valid_from(timed, ELAPSED)
    request = prepare(timed, projection_service)
    with owner(): output_review = projection_service.record(request, now=1200)
    cp_key, node_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with timed[0]._read() as (_, floor):
        ledger = PolicyLedger(tmp_path / "ledger.db", identity=NodeIdentity.parse(timed[0].binding.model_dump()),
            protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=timed[0].path, cp_issuer_id="cp-issuer",
        frontend_client_id="owner-ui", trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    now = [AS_OF]
    release = FactProjectionRelease(protocol=protocol, projections=projection_service, clock=lambda: now[0])
    return release, stated_day_policy(timed), cp_key, node_key, now, timed, output_review


def test_stated_day_release_is_signed_under_the_v2_capability(stated_setup):
    envelope, payload = issue(stated_setup)
    assert isinstance(envelope, SignedFactEnvelope) and envelope.capability_version == CAPABILITY_STATED_DAY
    [(result, output)] = dispatch(stated_setup, envelope, payload)
    assert output == OUTPUT and result["authority"]["capability_version"] == CAPABILITY_STATED_DAY
    verify_node_result(result, trusted_keys={"node-key": stated_setup[3].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)},
        envelope=envelope, output=output, now=AS_OF)
    with pytest.raises(PolicyError, match="request_replay"): dispatch(stated_setup, envelope, payload)
    with stated_setup[0].protocol.ledger._transaction() as db:
        stored = " ".join(str(tuple(r)) for r in db.execute("SELECT * FROM p2a_receipts"))
    assert "history books" not in stored and "I enjoy" not in stored


def test_v1_grant_withholds_the_same_stated_day_fact_at_release(stated_setup):
    envelope, payload = issue(stated_setup, change=to_v1)
    assert envelope.capability_version == CAPABILITY
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(stated_setup, envelope, payload, send=lambda *_: pytest.fail("a v1 grant released a stated-day fact"))


def test_pending_day_is_withheld_at_release_until_it_elapses_everywhere(stated_setup):
    release, raw, _, _, now, corpus, review = stated_setup
    set_valid_from(corpus, PENDING)
    attest(corpus, review_id="evidence-restamped")
    from topos.permissions_v2.evidence_reviews import EvidenceLookup
    from topos.permissions_v2.projection_reviews import RecordProjectionReview
    with owner():
        preview = release.projections.preview(EvidenceLookup(fact_id=corpus[2]), now=AS_OF)
        release.projections.record(RecordProjectionReview(review_id="output-restamped", expected_candidate=preview.candidate,
            expected_candidate_hash=preview.candidate_hash, expected_current_review_revision=review.review_revision,
            classification={"domains": ["reading"], "sensitivity": "personal", "subject": "self", "assertion": "explicit_atomic_preference"}), now=AS_OF)
    envelope, payload = issue(stated_setup, change=lambda p: p["validity"].update(expires_at=AS_OF + 6 * 3600))
    with pytest.raises(PolicyError, match="permission_denied"):
        dispatch(stated_setup, envelope, payload, send=lambda *_: pytest.fail("an unelapsed day was released"))
    now[0] = PENDING_ELAPSES_AT
    later, payload = issue(stated_setup, change=lambda p: p["validity"].update(expires_at=AS_OF + 6 * 3600), request_id="fact-read-2")
    [(_, output)] = dispatch(stated_setup, later, payload, request_id="fact-read-2")
    assert output == OUTPUT


def test_v2_cannot_replace_a_v1_grant_in_place(stated_setup):
    release, raw, _, _, now, _, _ = stated_setup
    with owner():
        release.protocol.ledger.activate(to_v1(deepcopy(raw)), grant_generation=1, assignment_generation=1, expected_epoch=0, command_id="v1", now=now[0])
        with pytest.raises(PolicyError, match="capability_change_requires_new_grant"):
            release.protocol.ledger.activate(raw, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="v2", now=now[0])


@pytest.mark.asyncio
async def test_offline_bridge_captures_v2_and_only_its_rules_arm_accepts_the_stated_day(timed, projection_service):
    set_valid_from(timed, ELAPSED)
    record_output(timed, projection_service)
    frozen = await shadow_bridge(timed, projection_service).run(timed[2], request_as_of=AS_OF, arm="rules_v2")
    current = await shadow_bridge(timed, projection_service, raw=stated_day_policy(timed)).run(timed[2], request_as_of=AS_OF, arm="rules_v2")
    assert [stage.verdict for stage in frozen.stages] == ["indeterminate"]
    assert [(stage.verdict, stage.reason_code) for stage in current.stages] == [("permit", "rule_permit")]
    assert current.model_calls == 0 and current.execution_enabled is False and current.serving_adapter is None
    model = Fake()
    semantic = await shadow_bridge(timed, projection_service, raw=stated_day_policy(timed), transport=model).run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
    assert semantic.verdict == "permit" and semantic.model_calls == 2
    assert all(ELAPSED not in call.candidate_data and "stated_day" not in call.candidate_data for call in model.calls)


def build_stated_day_golden(directory):
    """Deterministic keys, synthetic rows and one elapsed stated day; no owner data."""
    directory = Path(directory).resolve(strict=True)  # the resolver rejects symlinked parents such as macOS /var
    canonical = directory / "canonical.db"
    binding = EvidenceBinding(environment_id="beta", node_id="node-1", resource_id="resource-1", owner_id="owner-1")
    with sqlite3.connect(canonical) as conn:
        apply_signal_objects_up(conn); apply_owner_only_records_v1_up(conn); apply_entity_blackhole_v1_up(conn); apply_wiki_lifecycle_v1_up(conn)
        conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO engine_config VALUES('user_id','owner-1')")
        apply_wiki_entities_v1_up(conn)
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) VALUES('owner-entity','person','Owner','owner',1)")
        conn.execute("CREATE TABLE conversation_messages(message_id TEXT, dataset_id TEXT, source_id TEXT, content TEXT, is_from_self INTEGER, deleted_at TEXT, owner_user_id TEXT, event_at TEXT)")
        conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,source_id TEXT,content TEXT,sender_type TEXT,deleted_at TEXT,conversation_id TEXT, event_at TEXT)")
        conn.execute("CREATE TABLE ai_chat_conversations(conversation_id TEXT,source_id TEXT,owner_user_id TEXT)")
        conn.execute("INSERT INTO conversation_messages VALUES('message-1','dataset-1','source-1','I enjoy reading history books.',1,NULL,'owner-1',?)", (utc(AS_OF - 100),))
        conn.commit()
        fact = FactStore(conn).assert_fact(subject_entity_id="self", predicate="prefers", object_value="history books", disclosure="scoped",
            source_refs=[{"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1", "record_id": "message-1"}], asserted_by="owner")
        conn.execute("UPDATE signal_objects SET valid_from=?", (ELAPSED,))
        conn.commit()
    ensure_protection_clock(canonical, owner_id="owner-1")
    resolver = EvidenceResolver(canonical, binding=binding)
    with owner():
        reviews = EvidenceReviewStore(directory / "reviews.db", resolver=resolver)
        outputs = ProjectionReviewStore(directory / "outputs.db", resolver=resolver)
    corpus_tuple = (resolver, reviews, fact["object_id"])
    projections = ProjectionReviewService(resolver, reviews, outputs)
    with owner(): projections.record(prepare(corpus_tuple, projections), now=1200)
    cp_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    node_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    cp_keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    with resolver._read() as (_, floor):
        ledger = PolicyLedger(directory / "ledger.db", identity=NodeIdentity.parse(binding.model_dump()), protection_revision=floor, trusted_keys=cp_keys)
    protocol = NodePolicyProtocol(ledger, canonical_database=canonical, cp_issuer_id="beta-cp", frontend_client_id="permissions-beta-web",
        trusted_cp_keys=cp_keys, node_signing_kid="node-key", node_signing_key=node_key)
    release = FactProjectionRelease(protocol=protocol, projections=projections, clock=lambda: AS_OF)
    raw = stated_day_policy(corpus_tuple)
    raw["policy_version_id"] = "fact-policy-stated-day-1"
    parsed = parse_policy(raw)
    with ledger._transaction() as db: floor = ledger._node(db)["protection_revision"]
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1, "policy_version_id": raw["policy_version_id"],
        "policy_hash": digest(parsed.model_dump()), "capability_version": CAPABILITY_STATED_DAY, "protection_revision": floor, "node_epoch": 1}
    command = sign_mutation(MutationBody.parse({"version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": "beta-cp",
        "audience_id": "node-1", "command_id": "activate-stated-day-1", "operation": "activate", "expected_epoch": 0, "authority": authority,
        "policy": raw, "owner_authorization": {"actor_id": "owner-1", "client_id": "permissions-beta-web"}, "issued_at": AS_OF, "expires_at": AS_OF + 120}), cp_key)
    ack = protocol.mutate(command.model_dump(), now=AS_OF)
    assert ack.outcome == "applied"
    with owner(): snapshot = ledger.authority_snapshot("grant-1", now=AS_OF)
    payload = {"query": "fact:" + fact["object_id"]}
    request = {**{key: raw["binding"][key] for key in Binding.model_fields}, "request_id": "fact-golden-2", "request_type": "permissions.v2.fact.read"}
    envelope = sign_envelope(FactEnvelopeBody.parse({**snapshot.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": "fact-golden-2", "request_type": "permissions.v2.fact.read",
        "request_hash": request_digest("permissions.v2.fact.read", payload), "issued_at": AS_OF, "expires_at": AS_OF + 100}), cp_key)
    captured = []
    with recipient():
        release.dispatch(envelope=envelope.model_dump(), payload=payload, request_id="fact-golden-2", send=lambda result, output: captured.append((result, output)))
    [(result, output)] = captured
    return {"policy": raw, "policy_canonical": canonical_bytes(parsed.model_dump()).decode("ascii"), "policy_hash": digest(parsed.model_dump()),
        "authority": snapshot.model_dump(), "envelope": envelope.model_dump(), "request": request, "payload": payload, "now": AS_OF,
        "cp_public_key_hex": cp_keys["cp-key"].hex(), "node_public_key_hex": node_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
        "result": result, "output": output, "mutation": command.model_dump(), "ack": ack.model_dump(), "command_hash": command_digest(command),
        "signing_text": {"envelope": signing_bytes(envelope).decode("ascii"), "result": node_result_signing_bytes(SignedNodeResult.parse(result)).decode("ascii"),
            "mutation": protocol_signing_bytes(command).decode("ascii"), "ack": protocol_signing_bytes(ack).decode("ascii")}}


def verify_stated_day_golden(golden):
    parsed = parse_policy(golden["policy"])
    assert type(parsed) is StatedDayFactPolicy
    assert canonical_bytes(parsed.model_dump()).decode("ascii") == golden["policy_canonical"] and digest(parsed.model_dump()) == golden["policy_hash"]
    with pytest.raises(PolicyError): FactPolicyV2.parse(golden["policy"])
    envelope = verify_envelope(golden["envelope"], trusted_keys={"cp-key": bytes.fromhex(golden["cp_public_key_hex"])},
        expected_authority=FactAuthorityBinding.parse(golden["authority"]), request=FactRequestContext.parse(golden["request"]),
        payload=golden["payload"], now=golden["now"])
    assert isinstance(envelope, SignedFactEnvelope) and envelope.capability_version == CAPABILITY_STATED_DAY
    keys = {"node-key": bytes.fromhex(golden["node_public_key_hex"])}
    result = verify_node_result(golden["result"], trusted_keys=keys, envelope=envelope, output=golden["output"], now=golden["now"])
    assert golden["output"] == OUTPUT
    mutation = SignedMutation.parse(golden["mutation"])
    ack = verify_ack(golden["ack"], trusted_keys=keys, issuer_id=envelope.node_id, audience_id="beta-cp", request=mutation, now=golden["now"])
    assert ack.state.authority.capability_version == CAPABILITY_STATED_DAY and command_digest(mutation) == golden["command_hash"]
    for key, value in {"envelope": signing_bytes(envelope), "result": node_result_signing_bytes(result),
            "mutation": protocol_signing_bytes(mutation), "ack": protocol_signing_bytes(ack)}.items():
        assert value.decode("ascii") == golden["signing_text"][key]
    return envelope


def test_stated_day_golden_verifies_and_pins_the_v2_capability():
    verify_stated_day_golden(json.loads(GOLDEN.read_text()))


def test_freshly_built_golden_verifies_the_same_way(tmp_path):
    verify_stated_day_golden(json.loads(json.dumps(build_stated_day_golden(tmp_path))))


if __name__ == "__main__" and "--write-golden" in sys.argv:
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        GOLDEN.write_text(json.dumps(build_stated_day_golden(scratch), indent=2, sort_keys=True) + "\n")
    print("wrote", GOLDEN)


@pytest.mark.parametrize("model", [StatedDayFactPolicy, StatedDayFactDecision])
def test_stated_day_schema_exports_are_frozen(model):
    path = GOLDEN.parent / (model.__name__ + ".schema.json")
    assert json.loads(path.read_text()) == model.model_json_schema()
