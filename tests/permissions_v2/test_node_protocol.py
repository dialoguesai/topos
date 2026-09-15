from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from topos.features.lifecycle.record_protection import RecordProtectionStore, protection_fingerprint
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.ledger import NodeIdentity, PolicyLedger
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.protection_clock import ensure_protection_clock, current_protection_revision
from topos.permissions_v2.protocol import AckBody, MutationBody, SignedAck, SignedMutation, StatusRequestBody, command_digest, sign_ack, sign_mutation, sign_status_request, verify_ack
from topos.permissions_v2.signing import EnvelopeBody, RequestContext, request_digest, sign_envelope
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from tests.permissions_v2.test_contract_and_ledger import decision, output, sample_policy


@pytest.fixture
def protocol(tmp_path):
    canonical = tmp_path / "canonical.db"
    with sqlite3.connect(canonical) as conn:
        conn.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
        apply_owner_only_records_v1_up(conn)
        apply_entity_blackhole_v1_up(conn)
        apply_wiki_lifecycle_v1_up(conn)
        conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO engine_config VALUES ('user_id','owner-1')")
        conn.execute("CREATE TABLE conversation_messages(message_id TEXT PRIMARY KEY, source_id TEXT, content TEXT)")
        conn.execute("INSERT INTO conversation_messages VALUES ('record-1','source-A','synthetic')")
        conn.commit()
    ensure_protection_clock(canonical, owner_id="owner-1")
    with sqlite3.connect(canonical) as conn:
        conn.execute("BEGIN")
        floor = current_protection_revision(conn, owner_id="owner-1")
    cp_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    node_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    keys = {"cp-key": cp_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    policy = sample_policy()
    identity = NodeIdentity.parse({key: value for key, value in policy["binding"].items() if key in NodeIdentity.model_fields})
    ledger = PolicyLedger(tmp_path / "ledger.db", identity=identity, protection_revision=floor, trusted_keys=keys)
    receiver = NodePolicyProtocol(ledger, canonical_database=canonical, cp_issuer_id="beta-cp", frontend_client_id="permissions-beta-web", trusted_cp_keys=keys, node_signing_kid="node-key", node_signing_key=node_key)
    return receiver, policy, cp_key, node_key


def status_request(protocol, *, request_id="status-1", command=None, now=1100):
    node, policy, cp_key, _ = protocol
    return sign_status_request(StatusRequestBody.parse({"version": "topos-policy-status-request/v2", "kid": "cp-key", "issuer_id": "beta-cp", "audience_id": node.ledger.identity.node_id, "request_id": request_id, "binding": policy["binding"], "command_id": command.command_id if command else None, "command_hash": command_digest(command) if command else None, "issued_at": now, "expires_at": now + 120}), cp_key)


def mutation(protocol, *, command_id="activate-1", operation="activate", epoch=0, generation=1, policy=None, revision=None, now=1100):
    node, fixture_policy, cp_key, _ = protocol
    policy = copy.deepcopy(policy or fixture_policy)
    with node.ledger._transaction() as conn:
        floor = node.ledger._node(conn)["protection_revision"]
    authority = {**policy["binding"], "grant_generation": generation, "assignment_generation": generation, "policy_version_id": policy["policy_version_id"], "policy_hash": digest(policy), "capability_version": policy["versions"]["capability"], "protection_revision": revision or floor, "node_epoch": epoch + 1}
    return sign_mutation(MutationBody.parse({"version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": "beta-cp", "audience_id": node.ledger.identity.node_id, "command_id": command_id, "operation": operation, "expected_epoch": epoch, "authority": authority, "policy": policy if operation == "activate" else None, "owner_authorization": {"actor_id": policy["binding"]["owner_id"], "client_id": "permissions-beta-web"}, "issued_at": now, "expires_at": now + 120}), cp_key)


def checked_ack(protocol, ack, request, now=1100):
    node_key = protocol[3]
    return verify_ack(ack.model_dump(), trusted_keys={"node-key": node_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}, issuer_id="node-1", audience_id="beta-cp", request=request, now=now)


def reopen(protocol):
    old, policy, cp_key, node_key = protocol
    with old.ledger._transaction() as conn:
        floor = old.ledger._node(conn)["protection_revision"]
    ledger = PolicyLedger(old.ledger.path, identity=old.ledger.identity, protection_revision=floor, trusted_keys=old.ledger.trusted_keys)
    return NodePolicyProtocol(ledger, canonical_database=old.canonical_database, cp_issuer_id=old.cp_issuer_id, frontend_client_id=old.frontend_client_id, trusted_cp_keys=old.trusted_cp_keys, node_signing_kid="node-key", node_signing_key=node_key)


def test_signed_activation_lost_ack_fresh_retry_and_restart(protocol):
    node = protocol[0]
    command = mutation(protocol)
    applied = checked_ack(protocol, node.mutate(command.model_dump(), now=1100), command)
    assert applied.outcome == "applied"
    assert applied.state.grant_state == "active"
    node = reopen(protocol)
    retry = mutation(protocol, now=1300)
    assert command_digest(retry) == command_digest(command)
    assert digest(retry.model_dump()) != digest(command.model_dump())
    repeated = checked_ack(protocol, node.mutate(retry.model_dump(), now=1300), retry, now=1300)
    assert repeated.outcome == "already_applied"
    assert repeated.receipt == applied.receipt
    assert repeated.state.node_epoch == 1
    with pytest.raises(PolicyError, match="ack_correlation"):
        checked_ack(protocol, repeated, command, now=1300)


def test_authenticated_status_reconciles_lost_ack_and_unknown_command(protocol):
    command = mutation(protocol)
    node = protocol[0]
    node.mutate(command.model_dump(), now=1100)
    request = status_request(protocol, command=command)
    ack = checked_ack(protocol, node.status(request.model_dump(), now=1100), request)
    assert ack.outcome == "status"
    assert ack.receipt.command_hash == command_digest(command)
    assert ack.state.authority == command.authority
    missing = mutation(protocol, command_id="unknown")
    request = status_request(protocol, command=missing)
    assert node.status(request.model_dump(), now=1100).reason_code == "command_unknown"


def test_cancel_before_activation_creates_tombstone_preventing_late_activation(protocol):
    node = protocol[0]
    activate = mutation(protocol)
    cancel = mutation(protocol, command_id="cancel-1", operation="revoke", generation=2)
    ack = checked_ack(protocol, node.mutate(cancel.model_dump(), now=1100), cancel)
    assert ack.state.grant_state == "revoked"
    assert ack.state.authority.grant_generation == 2
    late = node.mutate(activate.model_dump(), now=1100)
    assert late.outcome == "rejected" and late.reason_code == "epoch_conflict"
    assert late.state.grant_state == "revoked"
    fresh = mutation(protocol, command_id="reactivate", epoch=1, generation=3)
    assert node.mutate(fresh.model_dump(), now=1100).outcome == "applied"


def test_activation_wins_cancel_race_then_reconciled_new_cancel_wins(protocol):
    node = protocol[0]
    activate = mutation(protocol)
    stale_cancel = mutation(protocol, command_id="cancel-1", operation="revoke", generation=2)
    node.mutate(activate.model_dump(), now=1100)
    conflict = node.mutate(stale_cancel.model_dump(), now=1100)
    assert conflict.reason_code == "epoch_conflict"
    request = status_request(protocol, command=activate)
    state = node.status(request.model_dump(), now=1100).state
    cancel = mutation(protocol, command_id="cancel-2", operation="revoke", epoch=state.node_epoch, generation=2)
    revoked = node.mutate(cancel.model_dump(), now=1100)
    assert revoked.state.grant_state == "revoked"
    delayed = node.mutate(activate.model_dump(), now=1100)
    assert delayed.outcome == "already_applied"
    assert delayed.receipt.authority.node_epoch == 1
    assert delayed.state.node_epoch == 2 and delayed.state.grant_state == "revoked"
    assert checked_ack(protocol, delayed, activate)


def test_concurrent_activation_and_cancellation_compare_and_set_once(protocol):
    node = protocol[0]
    commands = [mutation(protocol), mutation(protocol, command_id="cancel", operation="revoke", generation=2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda command: node.mutate(command.model_dump(), now=1100), commands))
    assert sorted(result.outcome for result in results) == ["applied", "rejected"]
    assert all(result.state.node_epoch == 1 for result in results)


def test_same_command_id_different_core_never_reinterpreted(protocol):
    node = protocol[0]
    command = mutation(protocol)
    node.mutate(command.model_dump(), now=1100)
    conflicting = mutation(protocol, operation="revoke", epoch=1, generation=2)
    ack = node.mutate(conflicting.model_dump(), now=1100)
    assert ack.reason_code == "command_conflict"
    assert ack.receipt is None and ack.state.grant_state == "active"


@pytest.mark.parametrize("field,value", [("issuer_id", "other-cp"), ("owner_authorization", {"actor_id": "owner-1", "client_id": "generic-oauth"}), ("issued_at", 1101), ("expires_at", 1100), ("expires_at", 1300), ("kid", "unknown-key")])
def test_validly_signed_wrong_authority_client_time_and_key_rejected(protocol, field, value):
    command = mutation(protocol).model_dump(exclude={"signature"})
    command[field] = value
    signed = sign_mutation(MutationBody.parse(command), protocol[2])
    with pytest.raises(PolicyError):
        protocol[0].mutate(signed.model_dump(), now=1100)


@pytest.mark.parametrize("field", ["environment_id", "node_id", "resource_id", "owner_id"])
def test_signed_mutation_cannot_cross_node_identity(protocol, field):
    policy = copy.deepcopy(protocol[1])
    policy["binding"][field] += "-other"
    command = mutation(protocol, policy=policy).model_dump(exclude={"signature"}) if field != "node_id" else None
    if field == "node_id":
        command = mutation(protocol).model_dump(exclude={"signature"})
        command["authority"][field] = policy["binding"][field]
        command["audience_id"] = policy["binding"][field]
        command["policy"] = policy
        command["authority"]["policy_hash"] = digest(policy)
    signed = sign_mutation(MutationBody.parse(command), protocol[2])
    with pytest.raises(PolicyError):
        protocol[0].mutate(signed.model_dump(), now=1100)


@pytest.mark.parametrize("field", list(SignedMutation.model_fields))
def test_mutation_missing_fields_rejected_without_legacy_fallback(protocol, field):
    command = mutation(protocol).model_dump()
    command.pop(field)
    with pytest.raises(PolicyError):
        protocol[0].mutate(command, now=1100)


def test_tampering_and_wrong_signature_domain_rejected(protocol):
    command = mutation(protocol).model_dump()
    command["command_id"] = "tampered"
    with pytest.raises(PolicyError, match="signature_invalid"):
        protocol[0].mutate(command, now=1100)
    status = status_request(protocol)
    with pytest.raises(PolicyError):
        protocol[0].mutate(status.model_dump(), now=1100)


def test_removed_cp_key_and_node_ack_key_rotation_require_current_pins(protocol):
    node = protocol[0]
    command = mutation(protocol)
    node.trusted_cp_keys.clear()
    with pytest.raises(PolicyError, match="signing_key_unknown"):
        node.mutate(command.model_dump(), now=1100)
    node.trusted_cp_keys = dict(node.ledger.trusted_keys)
    ack = node.mutate(command.model_dump(), now=1100)
    with pytest.raises(PolicyError, match="signing_key_unknown"):
        verify_ack(ack.model_dump(), trusted_keys={}, issuer_id="node-1", audience_id="beta-cp", request=command, now=1100)


def test_status_binding_and_ack_request_correlation(protocol):
    node = protocol[0]
    command = mutation(protocol)
    node.mutate(command.model_dump(), now=1100)
    request = status_request(protocol, command=command)
    ack = node.status(request.model_dump(), now=1100)
    other_request = status_request(protocol, request_id="other-request", command=command)
    with pytest.raises(PolicyError, match="ack_correlation"):
        checked_ack(protocol, ack, other_request)
    altered = request.model_dump(exclude={"signature"})
    altered["binding"]["client_id"] = "other-client"
    signed = sign_status_request(StatusRequestBody.parse(altered), protocol[2])
    with pytest.raises(PolicyError, match="binding_conflict"):
        node.status(signed.model_dump(), now=1100)


def protect(protocol):
    with sqlite3.connect(protocol[0].canonical_database) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="record-1")


def test_actual_owner_protection_changes_epoch_before_status_or_mutation_claim(protocol):
    node = protocol[0]
    command = mutation(protocol)
    node.mutate(command.model_dump(), now=1100)
    old = command.authority.protection_revision
    protect(protocol)
    status = status_request(protocol, command=command)
    ack = node.status(status.model_dump(), now=1100)
    assert ack.state.protection_revision != old
    assert ack.state.node_epoch == 2
    assert ack.receipt.authority.node_epoch == 1
    repeated = node.mutate(command.model_dump(), now=1100)
    assert repeated.outcome == "already_applied" and repeated.state.node_epoch == 2


def test_actual_protection_race_invalidates_inflight_grantee_checkpoint(protocol):
    node, policy, cp_key, _ = protocol
    command = mutation(protocol)
    node.mutate(command.model_dump(), now=1100)
    request = RequestContext.parse({**policy["binding"], "request_id": "grantee-request", "request_type": "permissions.v2.preview"})
    payload = {"query": "books"}
    envelope = sign_envelope(EnvelopeBody.parse({**command.authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key", "request_id": request.request_id, "request_type": request.request_type, "request_hash": request_digest(request.request_type, payload), "issued_at": 1100, "expires_at": 1200}), cp_key)
    lease = node.admit(envelope.model_dump(), request=request, payload=payload, now=1100)
    protect(protocol)
    with pytest.raises(PolicyError, match="authority_stale"):
        node.checkpoint_decision(lease, decision(policy), candidate_revision="b"*64, output=output(), now=1101)


def test_protection_changed_before_command_rejects_stale_epoch_and_preserves_floor(protocol):
    command = mutation(protocol)
    protect(protocol)
    ack = protocol[0].mutate(command.model_dump(), now=1100)
    assert ack.outcome == "rejected" and ack.reason_code == "epoch_conflict"
    assert ack.state.node_epoch == 1 and ack.state.grant_state == "absent"
    assert ack.state.protection_revision != command.authority.protection_revision


def test_protection_schema_missing_after_migration_fails_closed(protocol):
    with sqlite3.connect(protocol[0].canonical_database) as conn:
        conn.execute("DROP TABLE owner_only_records")
    with pytest.raises(PolicyError, match="protection_schema_unavailable"):
        protocol[0].status(status_request(protocol).model_dump(), now=1100)


def test_actual_owner_change_invalidates_protocol_before_effective_state_claim(protocol):
    with sqlite3.connect(protocol[0].canonical_database) as conn:
        conn.execute("UPDATE engine_config SET value='different-owner' WHERE key='user_id'")
    with pytest.raises(PolicyError, match="node_owner_binding"):
        protocol[0].status(status_request(protocol).model_dump(), now=1100)


def test_protocol_golden_and_generated_schemas_are_exact():
    from pathlib import Path
    from topos.permissions_v2.protocol import SignedStatusRequest, NodeGrantState, AppliedCommandReceipt, protocol_signing_bytes
    from topos.permissions_v2.runtime import NodeProtocolConfig
    fixtures = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2"
    golden = json.loads((fixtures / "protocol-golden-v1.json").read_text())
    for name, model in (("mutation", SignedMutation), ("status_request", SignedStatusRequest), ("ack", SignedAck)):
        parsed = model.parse(golden[name])
        assert protocol_signing_bytes(parsed).decode() == golden["signing_text"][name]
    request = SignedMutation.parse(golden["mutation"])
    assert command_digest(request) == golden["command_hash"]
    assert verify_ack(golden["ack"], trusted_keys={"node-key": bytes.fromhex(golden["node_public_key_hex"])}, issuer_id="node-1", audience_id="beta-cp", request=request, now=1100)
    for model in (SignedMutation, SignedStatusRequest, SignedAck, NodeGrantState, AppliedCommandReceipt, NodeProtocolConfig):
        assert json.loads((fixtures / f"{model.__name__}.schema.json").read_text()) == model.model_json_schema()


@pytest.mark.parametrize("case", ["rejected_receipt", "future_epoch", "future_time", "absent_state", "applied_wrong_state"])
def test_contradictory_ack_receipts_fail_closed(protocol, case):
    command = mutation(protocol)
    ack = protocol[0].mutate(command.model_dump(), now=1100).model_dump()
    if case == "rejected_receipt":
        ack.update(outcome="rejected", reason_code="epoch_conflict")
    elif case == "future_epoch":
        ack["receipt"]["authority"]["node_epoch"] = 2
    elif case == "future_time":
        ack["receipt"]["applied_at"] = 1101
    elif case == "absent_state":
        ack["state"].update(grant_state="absent", authority=None)
    else:
        ack["state"]["grant_state"] = "revoked"
    with pytest.raises(PolicyError, match="schema_invalid"):
        SignedAck.parse(ack)


def test_ack_verification_reparses_nested_request_mutation(protocol):
    command = mutation(protocol)
    ack = protocol[0].mutate(command.model_dump(), now=1100)
    command.policy.rules[0].evidence_use.sources.values.append("unregistered-source")
    with pytest.raises(PolicyError):
        checked_ack(protocol, ack, command)


def test_freshly_signed_retry_survives_signer_rotation_but_old_pin_is_revoked(protocol):
    node = protocol[0]
    command = mutation(protocol)
    node.mutate(command.model_dump(), now=1100)
    new_key = Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))
    node.trusted_cp_keys = {"new-cp-key": new_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
    fresh_body = command.model_dump(exclude={"signature"})
    fresh_body.update(kid="new-cp-key", issued_at=1300, expires_at=1420)
    retry = sign_mutation(MutationBody.parse(fresh_body), new_key)
    assert command_digest(retry) == command_digest(command)
    assert node.mutate(retry.model_dump(), now=1300).outcome == "already_applied"
    with pytest.raises(PolicyError, match="signing_key_unknown"):
        node.mutate(command.model_dump(), now=1100)


def test_protection_lift_does_not_reuse_old_epoch_even_when_fingerprint_returns(protocol):
    node = protocol[0]
    initial = node.status(status_request(protocol).model_dump(), now=1100).state
    protect(protocol)
    protected = node.status(status_request(protocol).model_dump(), now=1100).state
    with sqlite3.connect(node.canonical_database) as conn:
        RecordProtectionStore(conn).unprotect(canonical_table="conversation_messages", record_id="record-1")
    lifted = node.status(status_request(protocol).model_dump(), now=1100).state
    assert initial.protection_revision != lifted.protection_revision
    assert [initial.node_epoch, protected.node_epoch, lifted.node_epoch] == [0, 1, 2]


def test_reopening_ledger_cannot_silently_recreate_a_lost_canonical_clock(protocol):
    from topos.permissions_v2.protection_clock import TABLE, TRIGGERS
    with sqlite3.connect(protocol[0].canonical_database) as conn:
        for trigger in TRIGGERS:
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(f"DROP TABLE {TABLE}")
    with pytest.raises(PolicyError, match="protection_clock_unavailable"):
        reopen(protocol)


def test_unobserved_protect_lift_roundtrip_still_invalidates_old_revision(protocol):
    node = protocol[0]
    initial = node.status(status_request(protocol).model_dump(), now=1100).state
    protect(protocol)
    with sqlite3.connect(node.canonical_database) as conn:
        RecordProtectionStore(conn).unprotect(canonical_table="conversation_messages", record_id="record-1")
    after = node.status(status_request(protocol).model_dump(), now=1100).state
    assert after.node_epoch == initial.node_epoch + 1
    assert after.protection_revision != initial.protection_revision


@pytest.mark.parametrize("change", ["identity", "generation"])
def test_canonical_clock_replacement_or_rollback_is_rejected(protocol, change):
    from topos.permissions_v2.protection_clock import TABLE
    protect(protocol)
    protocol[0].status(status_request(protocol).model_dump(), now=1100)
    with sqlite3.connect(protocol[0].canonical_database) as conn:
        if change == "identity":
            conn.execute(f"UPDATE {TABLE} SET clock_id=?", ("f"*64,))
        else:
            conn.execute(f"UPDATE {TABLE} SET generation=0")
    with pytest.raises(PolicyError, match="protection_clock_rollback"):
        reopen(protocol)
