"""The signed identity command, end to end through the real dispatcher.

A read never enrolls and never consumes. A write is consumed exactly once, and
the ack the control plane gets back is signed by the node, so the control plane
learns what the node did rather than being told.
"""
import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_node_protocol import protocol  # noqa: F401
from tests.permissions_v2.test_protocol_runtime import configured  # noqa: F401
from topos.core.handlers import handle_control_plane_request
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.identity_protocol import (ATTESTATION_SENTENCE, AttestIdentity, DescribeIdentity,
    IdentityCommandBody, RevokeIdentity, sign_identity_command, verify_identity_ack)
from topos.permissions_v2.runtime import load_runtime
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

NOW = 1100


def command(runtime, cp_key, request, *, command_id="identity-command-1", client_id="permissions-beta-web",
            actor_id="owner-1"):
    identity = runtime.protocol.ledger.identity
    return sign_identity_command(IdentityCommandBody.parse({
        "version": "topos-owner-identity-command/v1", "kid": "cp-key", "issuer_id": "beta-cp",
        "audience_id": identity.node_id, "command_id": command_id, "binding": identity.model_dump(),
        "owner_authorization": {"actor_id": actor_id, "client_id": client_id},
        "request": request.model_dump(), "issued_at": NOW, "expires_at": NOW + 100}), cp_key)


@pytest.fixture
def live(configured, monkeypatch):
    from topos.permissions_v2 import runtime as runtime_module
    from topos.permissions_v2 import identity_dispatch
    _, path, fixture = configured
    runtime = load_runtime(path, active_database=fixture[0].canonical_database)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_IDENTITY_ATTESTATIONS_ENABLED", "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
    monkeypatch.setattr(identity_dispatch, "time", SimpleNamespace(time=lambda: NOW))
    try:
        yield runtime, fixture[2]
    finally:
        runtime.close()


async def send(envelope, *, principal=None):
    principal = principal or Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1",
                                       client_id="permissions-beta-web")
    return await handle_control_plane_request(
        {"id": "req", "type": "permissions_v2_identity_command", "payload": {"envelope": envelope.model_dump()}},
        principal=principal)


def node_public(runtime):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return {runtime.protocol.node_signing_kid:
            runtime.protocol.node_signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}


def ack_of(response, runtime, request_envelope):
    return verify_identity_ack(response["payload"]["ack"], trusted_keys=node_public(runtime),
                               issuer_id=runtime.protocol.ledger.identity.node_id, audience_id="beta-cp",
                               request=request_envelope, now=NOW)


@pytest.mark.asyncio
async def test_describe_returns_a_node_signed_state_and_writes_nothing(live):
    import sqlite3
    from topos.permissions_v2.protection_clock import LEDGER, TABLE
    runtime, cp_key = live
    canonical = runtime.protocol.canonical_database
    with sqlite3.connect(canonical) as conn:
        before = (conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0],
                  conn.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0])
    envelope = command(runtime, cp_key, DescribeIdentity())
    response = await send(envelope)
    assert response["status"] == "ok"
    ack = ack_of(response, runtime, envelope)
    assert ack.operation == "describe" and ack.error_code is None
    assert ack.result.contract == "owner_attested_v1"
    with sqlite3.connect(canonical) as conn:
        assert (conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0],
                conn.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0]) == before


def add_entity_spine(runtime, entity_id="owner-entity"):
    """The node gains the entity spine after the clock was installed.

    That is a coverage change, so every read fails closed until the resync lane
    installs the identity triggers. Doing it here exercises the real sequence a
    node goes through rather than a database that always had both.
    """
    import sqlite3
    from topos.permissions_v2.protection_clock import TABLE, resync_identity_coverage
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
    canonical = runtime.protocol.canonical_database
    with sqlite3.connect(canonical) as conn:
        clock = conn.execute(f"SELECT clock_id,generation FROM {TABLE} WHERE singleton=1").fetchone()
        apply_wiki_entities_v1_up(conn)
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
                     "VALUES(?,'person','Person','person',1)", (entity_id,))
    resync_identity_coverage(canonical, owner_id=runtime.protocol.ledger.identity.owner_id,
                             expected_clock_id=clock[0], expected_generation=clock[1])
    return entity_id


@pytest.mark.asyncio
async def test_a_node_without_the_entity_spine_describes_an_empty_state(live):
    runtime, cp_key = live
    envelope = command(runtime, cp_key, DescribeIdentity())
    ack = ack_of(await send(envelope), runtime, envelope)
    assert ack.result.subjects == [] and ack.result.permitted_count == 1
    assert not ack.result.literal_self_shadowed


@pytest.mark.asyncio
async def test_an_attestation_round_trip_is_signed_consumed_and_visible(live):
    runtime, cp_key = live
    add_entity_spine(runtime)
    described = ack_of(await send(command(runtime, cp_key, DescribeIdentity())), runtime,
                       command(runtime, cp_key, DescribeIdentity()))
    subject = described.result.subjects[0]
    request = AttestIdentity(entity_id=subject.entity_id, statement_version="owner-identity-attestation/v1",
        statement=ATTESTATION_SENTENCE, expected_entity_type=subject.entity_type, expected_is_self=1,
        expected_contact_id=subject.contact_id, expected_composition_revision=subject.composition_revision,
        replaces_entry_id=None)
    envelope = command(runtime, cp_key, request, command_id="identity-command-attest")
    ack = ack_of(await send(envelope), runtime, envelope)
    assert ack.error_code is None and ack.result.state == "active"
    # Replaying the identical signed command must not write a second entry. It
    # is refused as a conflict rather than as a replay, because the live entry
    # it would duplicate is checked before the command id is burned; the ledger
    # uniqueness is what catches a replay once that entry has been revoked.
    replay = ack_of(await send(envelope), runtime, envelope)
    assert replay.result is None and replay.error_code == "identity_attestation_conflict"
    after = ack_of(await send(command(runtime, cp_key, DescribeIdentity(), command_id="identity-command-3")),
                   runtime, command(runtime, cp_key, DescribeIdentity(), command_id="identity-command-3"))
    assert after.result.permitted_count == 2
    entry = next(item for item in after.result.subjects if item.entity_id == subject.entity_id)
    assert entry.entry_state == "active"
    # And revoking it, with the entry the owner is looking at.
    revoke = command(runtime, cp_key, RevokeIdentity(entity_id=subject.entity_id, entry_id=entry.entry_id),
                     command_id="identity-command-revoke")
    revoked = ack_of(await send(revoke), runtime, revoke)
    assert revoked.result.state == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [
    Principal(cls=THIRD_PARTY, channel="cp_relay", acting_user="owner-1", client_id="permissions-beta-web"),
    Principal(cls=OWNER_APP, channel="cp_relay", acting_user="someone-else", client_id="permissions-beta-web"),
    Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1", client_id="another-client"),
])
async def test_only_the_owners_own_frontend_reaches_the_identity_channel(live, principal):
    runtime, cp_key = live
    response = await send(command(runtime, cp_key, DescribeIdentity()), principal=principal)
    assert response["status"] == "error" and response["code"] in (403, 404)
    assert "ack" not in response.get("payload", {})


@pytest.mark.asyncio
async def test_a_command_the_control_plane_did_not_sign_is_refused(live):
    runtime, cp_key = live
    envelope = command(runtime, cp_key, DescribeIdentity())
    tampered = envelope.model_dump()
    tampered["command_id"] = "changed-after-signing"
    response = await handle_control_plane_request(
        {"id": "req", "type": "permissions_v2_identity_command", "payload": {"envelope": tampered}},
        principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1",
                            client_id="permissions-beta-web"))
    assert response["status"] == "error" and response["error"] == "identity_authority_invalid"


@pytest.mark.asyncio
async def test_the_channel_is_absent_until_its_own_flag_is_set(live, monkeypatch):
    runtime, cp_key = live
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_IDENTITY_ATTESTATIONS_ENABLED")
    envelope = command(runtime, cp_key, DescribeIdentity())
    response = await send(envelope)
    # The node answers with a signed ack carrying the reason, not an open door.
    ack = ack_of(response, runtime, envelope)
    assert ack.result is None and ack.error_code == "identity_attestations_disabled"


@pytest.mark.asyncio
async def test_a_malformed_payload_never_reaches_the_service(live):
    for payload in ({}, {"envelope": None, "extra": 1}, {"command": {}}):
        response = await handle_control_plane_request(
            {"id": "req", "type": "permissions_v2_identity_command", "payload": payload},
            principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1",
                                client_id="permissions-beta-web"))
        assert response["status"] == "error" and response["code"] in (400, 403)


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [
    None,
    Principal(cls=THIRD_PARTY, channel="cp_relay", acting_user="owner-1", client_id="permissions-beta-web"),
    Principal(cls=OWNER_APP, channel="http", acting_user="owner-1", client_id="permissions-beta-web"),
])
async def test_the_dispatch_itself_refuses_a_non_owner_channel(live, principal):
    """The handler registry is one gate. The dispatch does not rely on it."""
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.identity_dispatch import execute_signed_identity_command
    runtime, cp_key = live
    envelope = command(runtime, cp_key, DescribeIdentity())
    with pytest.raises(PolicyError, match="owner_authority_required"):
        await execute_signed_identity_command(envelope.model_dump(), principal=principal)
