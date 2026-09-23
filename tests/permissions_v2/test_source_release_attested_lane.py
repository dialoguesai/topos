"""p2a-v2 through every door a real owner and control plane use.

The work canary's lane: a synthetic chat.db holds one owner sentence, "I work at
Northwind <hex12>.", and every owner step is a CP-signed command or an owner
review handler (identity describe/attest/revoke, snapshot ingest, evidence
review). Grants are activated by CP-signed status/mutate, envelopes carry the
authority a signed status returned, and the recipient read is a CP-stamped frame
into the shipped source-message transport. The adapter is also driven directly,
as that transport builds it, where the refusal reason is visible.

Fixture state written directly (not part of the proof): the second ``is_self``
row, as the canary's own ambiguity control inserts it.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    CP_ISSUER, FRONTEND, SECOND_SELF, SELF_ENTITY, _next, attest_selves, canonical, corpus, facts, identity_command,
    lane, owner_message, owner_messages, paired_runtime, projection_runtime, protocol_call, review_evidence, run_lane)
from tests.permissions_v2.test_owner_identity_binding import add_entity
from tests.permissions_v2.test_release import dispatch
from tests.permissions_v2.test_release_transport import Socket
from tests.permissions_v2.test_source_release_attested import V1, V2, as_v2
from tests.permissions_v2.test_source_release_sibling_lane import source_policy
from topos.permissions_v2 import release_transport
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence import ReviewedClassification
from topos.permissions_v2.evidence_reviews import OwnerEvidencePreview, RecordEvidenceReview
from topos.permissions_v2.identity_protocol import DescribeIdentity, RevokeIdentity
from topos.permissions_v2.protocol import MutationBody, StatusRequestBody, sign_mutation, sign_status_request
from topos.permissions_v2.release import SourceMessageRelease
from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
from topos.relay_stamp import canonical_signing_payload


async def status(lane, binding):
    now = int(time.time())
    identity = lane.runtime.protocol.ledger.identity
    return await protocol_call(lane, "status", sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "request_id": _next(lane, "status"), "binding": binding,
        "command_id": None, "command_hash": None, "issued_at": now, "expires_at": now + 100}), lane.cp_key))


async def source_grant(lane, *, capability, name):
    """test_source_release_sibling_lane's signed status/mutate grant, as its own grant, under either capability."""
    now = int(time.time())
    raw = source_policy(lane, now)
    raw["binding"].update(grant_id=f"grant-{name}", assignment_id=f"assignment-{name}")
    raw["policy_version_id"] = f"attested-source-policy-{name}"
    if capability == V2:
        raw = as_v2(raw)
    identity = lane.runtime.protocol.ledger.identity
    state = (await status(lane, raw["binding"])).state
    authority = {**raw["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": raw["policy_version_id"], "policy_hash": digest(raw),
                 "capability_version": capability, "protection_revision": state.protection_revision,
                 "node_epoch": state.node_epoch + 1}
    ack = await protocol_call(lane, "mutate", sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "command_id": _next(lane, "activate"), "operation": "activate",
        "expected_epoch": state.node_epoch, "authority": authority, "policy": raw,
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": FRONTEND},
        "issued_at": now, "expires_at": now + 100}), lane.cp_key))
    assert (ack.outcome, ack.reason_code) == ("applied", "ok")
    assert ack.receipt.authority.capability_version == capability
    return raw["binding"]


async def current_authority(lane, binding):
    """What a CP learns before it signs: the node's current authority for this grant."""
    state = (await status(lane, binding)).state
    assert state.grant_state == "active"
    return state.authority


def envelope_for(lane, authority, fact_id, *, request_id, issued):
    payload = {"query": "fact:" + fact_id}
    body = parse_envelope({**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key",
        "request_id": request_id, "request_type": "permissions.v2.read",
        "request_hash": request_digest("permissions.v2.read", payload), "issued_at": issued, "expires_at": issued + 100},
        signed=False)
    return sign_envelope(body, lane.cp_key), payload


async def socket_read(lane, authority, fact_id, *, request_id, monkeypatch):
    """The recipient's door: a CP-stamped frame into the shipped source-message dispatcher."""
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(lane.cp_key.public_key().public_bytes_raw()).decode())
    now = int(time.time())
    envelope, payload = envelope_for(lane, authority, fact_id, request_id=request_id, issued=now)
    message = {"id": request_id, "type": release_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope.model_dump(), "intent": payload}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-1", "acting_user": "actor-1", "iat": now, "exp": now + 100}
    stamp["sig"] = base64.b64encode(lane.cp_key.sign(canonical_signing_payload(
        stamp, msg_id=request_id, msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    socket = Socket()
    await release_transport.dispatch_source_message(socket, message)
    [frame] = socket.sent
    return frame


def adapter_read(lane, authority, fact_id, *, request_id):
    """SourceMessageRelease exactly as the transport builds it. Returns (outputs, refusal code)."""
    service = lane.runtime.evidence_reviews(require_existing=True)
    adapter = SourceMessageRelease(protocol=lane.runtime.protocol, resolver=service.resolver,
                                   reviews=service.reviews, clock=lambda: int(time.time()))
    envelope, payload = envelope_for(lane, authority, fact_id, request_id=request_id, issued=int(time.time()))
    try:
        return dispatch((adapter,), envelope, payload, request_id=request_id), None
    except PolicyError as exc:
        return [], exc.code


async def reviewed_work_fact(lane):
    await attest_selves(lane, [SELF_ENTITY])
    await run_lane(lane, owner_messages(lane))
    [fact] = facts(lane, active_only=True)
    assert fact.payload["subject_entity_id"] == SELF_ENTITY
    await review_evidence(fact.object_id)
    return fact


async def review_again(fact_id, review_id):
    """The owner reviews the current evidence once more, replacing the review they recorded before."""
    preview = OwnerEvidencePreview.parse(await owner_message("evidence", "preview", {"fact_id": fact_id}))
    assert preview.status == "complete", preview.reason_code
    classifications = [ReviewedClassification(evidence=version, domains=["work"], sensitivity="personal",
        subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
        independent_copies="none_known") for version in preview.snapshot.artifacts + preview.snapshot.leaves]
    await owner_message("evidence", "review_record", RecordEvidenceReview(review_id=review_id,
        expected_snapshot=preview.snapshot, expected_current_review_revision=preview.current_review_revision,
        classifications=classifications).model_dump())


def records(lane):
    return [{"record_id": "imessage:1", "source_id": "imessage", "canonical_table": "conversation_messages",
             "content": f"I work at {lane.employer}."}]


REFUSED = {"type": release_transport.MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}


@pytest.mark.asyncio
async def test_two_attested_selves_release_the_raw_message_under_v2_and_not_v1(lane, monkeypatch):
    fact = await reviewed_work_fact(lane)
    # An entity resolver later creates a second self row, and the owner attests it too.
    with canonical(lane) as conn:
        add_entity(conn, SECOND_SELF)
    state = await attest_selves(lane, [SECOND_SELF])
    assert {item.entity_id: item.entry_state for item in state.subjects} == {SELF_ENTITY: "active", SECOND_SELF: "active"}
    v2 = await source_grant(lane, capability=V2, name="v2")
    v1 = await source_grant(lane, capability=V1, name="v1")

    frame = await socket_read(lane, await current_authority(lane, v2), fact.object_id, request_id="lane-v2-socket-1",
                              monkeypatch=monkeypatch)
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"] == records(lane)
    assert frame["payload"]["result"]["authority"]["capability_version"] == V2
    for private in (SELF_ENTITY, SECOND_SELF):
        assert private not in json.dumps(frame)

    authority = await current_authority(lane, v1)
    assert adapter_read(lane, authority, fact.object_id, request_id="lane-v1-adapter-1") == ([], "owner_subject_ambiguous")
    assert await socket_read(lane, authority, fact.object_id, request_id="lane-v1-socket-1",
                             monkeypatch=monkeypatch) == {"id": "lane-v1-socket-1", **REFUSED}


@pytest.mark.asyncio
async def test_withdrawing_the_attestation_by_signed_command_stops_v2_and_leaves_v1_releasing(lane, monkeypatch):
    fact = await reviewed_work_fact(lane)
    v2 = await source_grant(lane, capability=V2, name="v2")
    v1 = await source_grant(lane, capability=V1, name="v1")
    for binding, request_id in ((v2, "lane-v2-socket-1"), (v1, "lane-v1-socket-1")):
        frame = await socket_read(lane, await current_authority(lane, binding), fact.object_id,
                                  request_id=request_id, monkeypatch=monkeypatch)
        assert frame["status"] == "ok" and frame["payload"]["output"]["records"] == records(lane), request_id

    described = await identity_command(lane, DescribeIdentity())
    [subject] = [item for item in described.subjects if item.entity_id == SELF_ENTITY]
    await identity_command(lane, RevokeIdentity(entity_id=SELF_ENTITY, entry_id=subject.entry_id))
    # The owner's review bound that attestation, so both capabilities refuse until
    # the owner looks again; afterwards only the withdrawal refuses, and only v2.
    for binding, request_id in ((v2, "lane-v2-adapter-2"), (v1, "lane-v1-adapter-2")):
        assert adapter_read(lane, await current_authority(lane, binding), fact.object_id,
                            request_id=request_id) == ([], "review_stale"), request_id
    await review_again(fact.object_id, "lane-evidence-review-2")

    authority = await current_authority(lane, v2)
    assert adapter_read(lane, authority, fact.object_id, request_id="lane-v2-adapter-3") == ([], "owner_subject_unattested")
    assert await socket_read(lane, authority, fact.object_id, request_id="lane-v2-socket-3",
                             monkeypatch=monkeypatch) == {"id": "lane-v2-socket-3", **REFUSED}
    frame = await socket_read(lane, await current_authority(lane, v1), fact.object_id, request_id="lane-v1-socket-3",
                              monkeypatch=monkeypatch)
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"] == records(lane)
