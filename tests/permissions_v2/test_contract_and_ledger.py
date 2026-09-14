"""Adversarial P2a boundary tests: real signing and a private SQLite ledger."""
from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from topos.permissions_v2.canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest, parse_json
from topos.permissions_v2.contract import CAPABILITY, VIEW, Decision, PolicyV2, capability_document, evaluate_predicate
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.signing import AuthorityBinding, EnvelopeBody, RequestContext, SignedEnvelope, request_digest, sign_envelope, signing_bytes, verify_envelope
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2"


def sample_policy():
    true = {"kind": "all_of", "terms": []}
    return {
        "version": "topos-policy/v2", "policy_version_id": "policy-1",
        "binding": {"environment_id": "beta", "node_id": "node-1", "resource_id": "resource-1", "owner_id": "owner-1", "actor_id": "actor-1", "client_id": "client-1", "grant_id": "grant-1", "assignment_id": "assignment-1"},
        "versions": {"vocabulary": "vocabulary-1", "capability": CAPABILITY},
        "validity": {"starts_at": 1000, "expires_at": 5000},
        "source_universe": {"universe_id": "sources-1", "revision": 1, "source_ids": ["source-A", "source-B"]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold", "unknown_lineage": "withhold", "cross_rule_derivation": "deny", "capability_growth": "require_consent"},
        "rules": [{"rule_id": "rule-A", "effect": "permit", "evidence_use": {"sources": {"kind": "only", "values": ["source-A"]}, "predicate": true, "purpose": "reading", "processors": {"kind": "only", "values": ["owner-engine-local"]}, "new_records": "include_if_predicate"}, "release": {"predicate": true, "ceiling": "raw", "forms": [{"family": "canonical_record", "operation": "read", "view_id": VIEW, "tables": ["conversation_messages"]}]}}],
        "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2a-v1"}, "natural_language": None,
    }


@pytest.fixture
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def setup(tmp_path, owner):
    policy = sample_policy()
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    keys = {"beta-key-1": key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    identity = NodeIdentity.parse({key: value for key, value in policy["binding"].items() if key in NodeIdentity.model_fields})
    ledger = PolicyLedger(tmp_path / "policy-v2.db", identity=identity, protection_revision="a" * 64, trusted_keys=keys)
    ledger.activate(policy, grant_generation=1, assignment_generation=1, expected_epoch=0, command_id="activate-1", now=1100)
    return ledger, policy, key, keys


def signed_request(setup, request_id="request-1", changes=None):
    ledger, policy, key, _ = setup
    authority = ledger.authority_snapshot(policy["binding"]["grant_id"], now=1100)
    request = RequestContext.parse({**policy["binding"], "request_id": request_id, "request_type": "permissions.v2.preview"})
    payload = {"question": "Café 📚 / line\n", "limit": 10, "empty": [], "literal": "e\u0301"}
    body = {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "beta-key-1", "request_id": request_id, "request_type": request.request_type, "request_hash": request_digest(request.request_type, payload), "issued_at": 1100, "expires_at": 1200}
    body.update(changes or {})
    return authority, request, payload, sign_envelope(EnvelopeBody.parse(body), key).model_dump()


def decision(policy, verdict="permit"):
    return {"stage": "output_release", "verdict": verdict, "policy_hash": digest(policy), "candidate_revision": "b" * 64, "evaluator_version": "hard-rules/p2a-v1", "matched_allow_clause_ids": ["rule-A"] if verdict == "permit" else [], "matched_deny_clause_ids": [], "reason_code": "rule_permit" if verdict == "permit" else "unknown_context", "required_projection_id": VIEW if verdict == "permit" else None, "missing_context_codes": [] if verdict == "permit" else ["lineage"]}


def output(source="source-A", table="conversation_messages"):
    return {"family": "canonical_record", "operation": "read", "view_id": VIEW, "records": [{"record_id": "record-1", "source_id": source, "canonical_table": table, "content": "Synthetic book note"}]}


def checkpoint(ledger, lease, policy, **kwargs):
    return ledger.checkpoint_decision(lease, kwargs.pop("decision", decision(policy)), candidate_revision=kwargs.pop("candidate_revision", "b" * 64), output=kwargs.pop("output", output()), now=kwargs.pop("now", 1101), **kwargs)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', '{"x":1.0}', '{"x":1e0}', '{"x":NaN}', '{"x":Infinity}', '{"x":9007199254740992}', '{"x":"\\ud800"}', '{"é":1}', b'\xff', '[' * 50 + '0' + ']' * 50])
def test_noncanonical_json_rejected(raw):
    with pytest.raises(PolicyError):
        parse_json(raw)


@pytest.mark.parametrize("value", [{"x": 1.0}, {"x": MAX_INTEGER + 1}, {"x": (1, 2)}, {1: "x"}, {"x": "\udfff"}])
def test_python_objects_cannot_bypass_strict_json(value):
    with pytest.raises(PolicyError):
        canonical_bytes(value)


def test_unicode_preserved_without_normalization_and_keys_sorted():
    assert canonical_bytes({"z": "é📚", "a": "\n"}) == b'{"a":"\\n","z":"\\u00e9\\ud83d\\udcda"}'
    assert digest({"x": "é"}) != digest({"x": "e\u0301"})
    assert parse_json('{"x":"\\ud83d\\udcda"}') == {"x": "📚"}


@pytest.mark.parametrize("path,value", [("unknown", 1), ("version", "topos-policy/v3"), ("natural_language", "allow books"), ("rules", None)])
def test_policy_unknown_fields_and_future_versions_fail_closed(path, value):
    policy = sample_policy()
    policy[path] = value
    with pytest.raises(PolicyError):
        PolicyV2.parse(policy)


@pytest.mark.parametrize("field", list(sample_policy()))
def test_every_policy_field_required(field):
    policy = sample_policy()
    policy.pop(field)
    with pytest.raises(PolicyError):
        PolicyV2.parse(policy)


def test_explicit_empty_sources_and_forms_preserved():
    policy = sample_policy()
    policy["rules"][0]["evidence_use"]["sources"]["values"] = []
    policy["rules"][0]["release"]["forms"] = []
    parsed = PolicyV2.parse(policy)
    assert parsed.rules[0].evidence_use.sources.values == []
    assert parsed.rules[0].release.forms == []


def test_universe_pin_and_no_future_source_growth():
    policy = sample_policy()
    policy["rules"][0]["evidence_use"]["sources"] = {"kind": "all", "universe_id": "sources-1", "universe_revision": 1, "growth": "require_consent"}
    assert PolicyV2.parse(policy)
    policy["rules"][0]["evidence_use"]["sources"]["universe_revision"] = 2
    with pytest.raises(PolicyError):
        PolicyV2.parse(policy)


@pytest.mark.parametrize("kind,expected", [("atom", None), ("not", None), ("all_of", None), ("any_of", None)])
def test_unknown_never_becomes_permission_under_boolean_composition(kind, expected):
    atom = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["books"]}
    predicate = atom if kind == "atom" else {"kind": kind, **({"term": atom} if kind == "not" else {"terms": [atom]})}
    policy = sample_policy()
    policy["rules"][0]["evidence_use"]["predicate"] = predicate
    parsed = PolicyV2.parse(policy).rules[0].evidence_use.predicate
    assert evaluate_predicate(parsed, {}) is expected
    assert evaluate_predicate(parsed, {"domain": ["books"]}) is (False if kind == "not" else True)


def test_capability_is_registry_only():
    assert capability_document()["registered_forms"] == [{"family": "canonical_record", "operation": "read", "view_id": VIEW}]
    assert capability_document()["executable_forms"] == []
    assert not capability_document()["natural_language"]
    assert not capability_document()["lineage_certified"]


def test_golden_schema_bytes_hash_signature_and_json_schema():
    golden = json.loads((FIXTURES / "golden-v1.json").read_text())
    policy = PolicyV2.parse(golden["policy"])
    assert canonical_bytes(policy.model_dump()).decode("ascii") == golden["policy_canonical"]
    assert digest(policy.model_dump()) == golden["policy_hash"]
    envelope = SignedEnvelope.parse(golden["envelope"])
    assert signing_bytes(envelope).decode("ascii") == golden["signing_text"]
    assert request_digest(golden["request"]["request_type"], golden["payload"]) == envelope.request_hash
    expected = AuthorityBinding.parse({key: golden["envelope"][key] for key in AuthorityBinding.model_fields})
    assert verify_envelope(golden["envelope"], trusted_keys={"beta-key-1": bytes.fromhex(golden["public_key_hex"])}, expected_authority=expected, request=RequestContext.parse(golden["request"]), payload=golden["payload"], now=1100)
    for model in (PolicyV2, SignedEnvelope, Decision):
        assert json.loads((FIXTURES / f"{model.__name__}.schema.json").read_text()) == model.model_json_schema()


@pytest.mark.parametrize("field", list(SignedEnvelope.model_fields))
def test_every_envelope_field_required(setup, field):
    authority, request, payload, envelope = signed_request(setup)
    envelope.pop(field)
    with pytest.raises(PolicyError):
        verify_envelope(envelope, trusted_keys=setup[3], expected_authority=authority, request=request, payload=payload, now=1100)


@pytest.mark.parametrize("field", list(AuthorityBinding.model_fields))
def test_even_valid_signature_cannot_cross_authority_binding(setup, field):
    authority, request, payload, envelope = signed_request(setup)
    value = envelope[field]
    if field == "capability_version":
        envelope[field] = "future-capability"
    else:
        envelope[field] = value + 1 if type(value) is int else "c" * 64 if field.endswith("hash") or field == "protection_revision" else value + "-other"
        body = {key: value for key, value in envelope.items() if key != "signature"}
        envelope = sign_envelope(EnvelopeBody.parse(body), setup[2]).model_dump()
    with pytest.raises(PolicyError):
        verify_envelope(envelope, trusted_keys=setup[3], expected_authority=authority, request=request, payload=payload, now=1100)


@pytest.mark.parametrize("changes", [{"issued_at": 1101}, {"expires_at": 1100}, {"expires_at": 1221}, {"issued_at": 1100, "expires_at": 1099}, {"kid": "other-key"}, {"request_id": "other-request"}, {"request_type": "permissions.v2.read"}, {"request_hash": "f" * 64}])
def test_expiry_key_and_actual_request_context(setup, changes):
    authority, request, payload, envelope = signed_request(setup, changes=changes)
    with pytest.raises(PolicyError):
        verify_envelope(envelope, trusted_keys=setup[3], expected_authority=authority, request=request, payload=payload, now=1100)


def test_tampering_rejected_and_not_legacy(setup):
    authority, request, payload, envelope = signed_request(setup)
    envelope["actor_id"] = "attacker"
    with pytest.raises(PolicyError, match="signature_invalid"):
        verify_envelope(envelope, trusted_keys=setup[3], expected_authority=authority, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError):
        setup[0].admit({}, request=request, payload=payload, now=1100)


def test_actual_payload_tamper_rejected(setup):
    _, request, payload, envelope = signed_request(setup)
    payload["limit"] = 11
    with pytest.raises(PolicyError, match="request_hash"):
        setup[0].admit(envelope, request=request, payload=payload, now=1100)


def test_positive_admission_checkpoint_private_and_replay_rejected(setup):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    receipt = checkpoint(ledger, lease, policy)
    assert receipt["verdict"] == "permit"
    assert receipt["execution_enabled"] is False
    assert "Synthetic book note" not in canonical_bytes(receipt).decode()
    assert receipt["output_hash"] == digest(output())
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit(envelope, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError, match="request_replay"):
        checkpoint(ledger, lease, policy)
    with sqlite3.connect(ledger.path) as conn:
        stored = conn.execute("SELECT decision_json FROM p2a_receipts").fetchone()[0]
        assert json.loads(stored) == decision(policy)


def test_replay_persists_after_restart(setup):
    ledger, _, _, keys = setup
    _, request, payload, envelope = signed_request(setup)
    ledger.admit(envelope, request=request, payload=payload, now=1100)
    reopened = PolicyLedger(ledger.path, identity=ledger.identity, protection_revision="a" * 64, trusted_keys=keys)
    with pytest.raises(PolicyError, match="request_replay"):
        reopened.admit(envelope, request=request, payload=payload, now=1100)


def test_concurrent_replay_only_one_admission(setup):
    ledger = setup[0]
    _, request, payload, envelope = signed_request(setup)
    def attempt(_):
        try:
            ledger.admit(envelope, request=request, payload=payload, now=1100)
            return "admitted"
        except PolicyError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sorted(results) == ["admitted", "request_replay"]


@pytest.mark.parametrize("cls", [None, THIRD_PARTY, "cp_relay", "grantee", "owner_automation"])
def test_owner_identity_or_grantee_cannot_activate_or_revoke(setup, cls):
    ledger, policy, _, _ = setup
    token = set_principal(None if cls is None else Principal(cls=cls, channel="local_http", acting_user="owner-1"))
    try:
        with pytest.raises(PolicyError, match="owner_required"):
            ledger.activate(policy, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="bad", now=1100)
        with pytest.raises(PolicyError, match="owner_required"):
            ledger.revoke("grant-1", expected_epoch=1, command_id="bad")
    finally:
        reset_principal(token)


def test_activation_idempotency_and_conflicting_reuse(setup):
    ledger, policy, _, _ = setup
    assert ledger.activate(policy, grant_generation=1, assignment_generation=1, expected_epoch=0, command_id="activate-1", now=1100) == {"applied_epoch": 1, "command_id": "activate-1"}
    with pytest.raises(PolicyError, match="idempotency_conflict"):
        ledger.activate(policy, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="activate-1", now=1100)
    with pytest.raises(PolicyError, match="epoch_conflict"):
        ledger.revoke("grant-1", expected_epoch=0, command_id="revoke-stale")
    ledger.revoke("grant-1", expected_epoch=1, command_id="revoke")
    # A delayed exact retry reports the historical operation, without reviving
    # revoked authority, even if the old policy has since expired.
    assert ledger.activate(policy, grant_generation=1, assignment_generation=1, expected_epoch=0, command_id="activate-1", now=6000)["applied_epoch"] == 1
    with pytest.raises(PolicyError, match="grant_inactive"):
        ledger.authority_snapshot("grant-1", now=1101)


def test_generation_monotonic_policy_immutable_and_binding_pinned(setup):
    ledger, policy, _, _ = setup
    with pytest.raises(PolicyError, match="generation_stale"):
        ledger.activate(policy, grant_generation=2, assignment_generation=1, expected_epoch=1, command_id="stale", now=1100)
    changed = copy.deepcopy(policy)
    changed["rules"][0]["evidence_use"]["sources"]["values"] = []
    with pytest.raises(PolicyError, match="immutable_policy"):
        ledger.activate(changed, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="mutate", now=1100)
    changed["policy_version_id"] = "policy-2"
    changed["binding"]["actor_id"] = "other"
    with pytest.raises(PolicyError, match="policy_binding"):
        ledger.activate(changed, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="rebind", now=1100)


@pytest.mark.parametrize("mutation", ["revoke", "protection", "update"])
def test_epoch_changes_reject_prior_envelope_before_admission_and_during_release(setup, mutation):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    _, second_request, second_payload, second_envelope = signed_request(setup, request_id="request-2")
    if mutation == "revoke":
        ledger.revoke("grant-1", expected_epoch=1, command_id="mutation")
    elif mutation == "protection":
        ledger.update_protection("c" * 64, expected_epoch=1, command_id="mutation")
    else:
        changed = copy.deepcopy(policy)
        changed["policy_version_id"] = "policy-2"
        ledger.activate(changed, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="mutation", now=1100)
    with pytest.raises(PolicyError):
        ledger.admit(second_envelope, request=second_request, payload=second_payload, now=1101)
    with pytest.raises(PolicyError):
        checkpoint(ledger, lease, policy)
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM p2a_receipts").fetchone()[0] == 0


def test_tombstone_prevents_old_activation_and_duplicate_revoke_is_idempotent(setup):
    ledger, policy, _, _ = setup
    first = ledger.revoke("grant-1", expected_epoch=1, command_id="revoke")
    assert ledger.revoke("grant-1", expected_epoch=1, command_id="revoke") == first
    with pytest.raises(PolicyError, match="generation_stale"):
        ledger.activate(policy, grant_generation=2, assignment_generation=2, expected_epoch=2, command_id="old", now=1100)
    ledger.activate(policy, grant_generation=3, assignment_generation=3, expected_epoch=2, command_id="new", now=1100)
    assert ledger.authority_snapshot("grant-1", now=1100).node_epoch == 3


@pytest.mark.parametrize("changes", [{"policy_hash": "c" * 64}, {"candidate_revision": "c" * 64}, {"stage": "evidence_use"}, {"matched_deny_clause_ids": ["deny-1"]}, {"missing_context_codes": ["lineage"]}, {"required_projection_id": None}])
def test_final_decision_must_match_policy_candidate_and_closed_projection(setup, changes):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    changed = {**decision(policy), **changes}
    with pytest.raises(PolicyError):
        checkpoint(ledger, lease, policy, decision=changed)


@pytest.mark.parametrize("changed", [output(source="source-B"), output(table="ai_chat_messages"), {**output(), "debug_raw": "secret"}, {**output(), "family": "fact"}])
def test_same_rule_source_table_and_output_schema_cannot_widen(setup, changed):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError):
        checkpoint(ledger, lease, policy, output=changed)


def test_two_rules_cannot_mix_source_from_a_with_output_from_b(setup):
    ledger, policy, _, _ = setup
    second = copy.deepcopy(policy["rules"][0])
    second["rule_id"] = "rule-B"
    second["evidence_use"]["sources"]["values"] = ["source-B"]
    second["release"]["forms"][0]["tables"] = ["ai_chat_messages"]
    policy["rules"].append(second)
    policy["policy_version_id"] = "policy-2"
    ledger.activate(policy, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="update", now=1100)
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    for clause_ids in (["rule-A"], ["rule-B"], ["rule-A", "rule-B"]):
        with pytest.raises(PolicyError, match="rule_binding|decision_inconsistent"):
            checkpoint(ledger, lease, policy, decision={**decision(policy), "matched_allow_clause_ids": clause_ids}, output=output(table="ai_chat_messages"))


def test_deny_and_indeterminate_receipts_never_accept_output(setup):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError, match="denied_output"):
        checkpoint(ledger, lease, policy, decision=decision(policy, "indeterminate"))
    receipt = checkpoint(ledger, lease, policy, decision=decision(policy, "indeterminate"), output=None)
    assert receipt["output_hash"] is None
    assert receipt["execution_enabled"] is False


def test_expiry_rechecked_after_evaluation(setup):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError, match="lease_expired"):
        checkpoint(ledger, lease, policy, now=1200)


def test_copied_ledger_cannot_be_opened_as_other_node(setup):
    ledger, _, _, keys = setup
    other = NodeIdentity.parse({**ledger.identity.model_dump(), "node_id": "other-node"})
    with pytest.raises(PolicyError, match="ledger_identity"):
        PolicyLedger(ledger.path, identity=other, protection_revision="a" * 64, trusted_keys=keys)


@pytest.mark.parametrize("channel,acting_user", [("uds", "other-owner"), ("local_http", "owner-1"), ("local_http", ""), ("cp_relay", "other-owner"), ("cp_relay", ""), ("unverified", "owner-1")])
def test_owner_class_still_requires_correct_ledger_owner_and_channel(setup, channel, acting_user):
    token = set_principal(Principal(cls=OWNER_APP, channel=channel, acting_user=acting_user))
    try:
        with pytest.raises(PolicyError, match="owner_binding|owner_channel"):
            setup[0].revoke("grant-1", expected_epoch=1, command_id="bad-owner")
    finally:
        reset_principal(token)


@pytest.mark.parametrize("channel,acting_user", [("uds", ""), ("uds", "owner-1"), ("cp_relay", "owner-1")])
def test_verified_owner_socket_and_bound_relay_owner_remain_usable(setup, channel, acting_user):
    token = set_principal(Principal(cls=OWNER_APP, channel=channel, acting_user=acting_user))
    try:
        assert setup[0].authority_snapshot("grant-1", now=1100).grant_generation == 1
    finally:
        reset_principal(token)


@pytest.mark.parametrize("replace_key", [False, True])
def test_signer_revocation_after_admission_rejects_final_checkpoint(setup, replace_key):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    new_keys = {"beta-key-1": bytes([42]) * 32} if replace_key else {}
    reopened = PolicyLedger(ledger.path, identity=ledger.identity, protection_revision="a" * 64, trusted_keys=new_keys)
    with pytest.raises(PolicyError, match="signing_key_unknown|signature_invalid"):
        checkpoint(reopened, lease, policy)


def test_wrong_signature_domain_cannot_replay_legacy_stamp(setup):
    import base64
    authority, request, payload, envelope = signed_request(setup)
    body = {key: value for key, value in envelope.items() if key != "signature"}
    envelope["signature"] = base64.urlsafe_b64encode(setup[2].sign(canonical_bytes(body))).decode().rstrip("=")
    with pytest.raises(PolicyError, match="signature_invalid"):
        verify_envelope(envelope, trusted_keys=setup[3], expected_authority=authority, request=request, payload=payload, now=1100)


def test_concurrent_owner_mutations_compare_and_set_once(setup):
    ledger = setup[0]
    def attempt(index):
        token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
        try:
            ledger.update_protection(str(index) * 64, expected_epoch=1, command_id=f"protection-{index}")
            return "applied"
        except PolicyError as exc:
            return exc.code
        finally:
            reset_principal(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sorted(results) == ["applied", "epoch_conflict"]


def test_global_epoch_also_invalidates_other_grants(setup):
    ledger, policy, _, _ = setup
    _, request, payload, envelope = signed_request(setup)
    other = copy.deepcopy(policy)
    other["binding"].update(grant_id="grant-2", assignment_id="assignment-2", client_id="client-2")
    other["policy_version_id"] = "policy-2"
    ledger.activate(other, grant_generation=1, assignment_generation=1, expected_epoch=1, command_id="other", now=1100)
    with pytest.raises(PolicyError, match="authority_binding"):
        ledger.admit(envelope, request=request, payload=payload, now=1100)


def test_explicit_empty_source_policy_cannot_checkpoint_permitted_rows(setup):
    ledger, policy, _, _ = setup
    policy["rules"][0]["evidence_use"]["sources"]["values"] = []
    policy["policy_version_id"] = "policy-empty"
    ledger.activate(policy, grant_generation=2, assignment_generation=2, expected_epoch=1, command_id="empty", now=1100)
    _, request, payload, envelope = signed_request(setup)
    lease = ledger.admit(envelope, request=request, payload=payload, now=1100)
    with pytest.raises(PolicyError, match="rule_binding"):
        checkpoint(ledger, lease, policy)


def test_schema_error_traceback_does_not_include_denied_candidate_data():
    import traceback
    from topos.permissions_v2.contract import MessageDisclosure
    private = "SYNTHETIC_PRIVATE_CANARY"
    try:
        MessageDisclosure.parse({**output(), "debug_raw": private})
    except PolicyError as exc:
        assert private not in "".join(traceback.format_exception(exc))
    else:
        pytest.fail("unexpected schema acceptance")
