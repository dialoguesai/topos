"""Closed owner-attested snapshot commands; distinct from policy and grantee proof.

Only paired keys and identities verify this channel. Native account ownership
is explicitly owner-attested; neither a file name nor a sent-by-me bit proves it.
This pure contract does not enroll a source, consume a command, or run ingestion.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from .canonical import PolicyError, digest
from .contract import Generation, Hash, Identifier, Number, StrictModel
from .protocol import NodeIdentity, OwnerAuthorization, Signature, _sign, _verify

OWNER_ATTESTATION = "I attest that this snapshot contains my iMessage account and that native sent-by-me messages are mine."
CHATGPT_OWNER_ATTESTATION = ("I attest that this ChatGPT export contains only my own ChatGPT account and that "
                             "the prompts it records as mine were written by me.")
IMESSAGE_READER_CONTRACT = "imessage-owner-snapshot/v1"
CHATGPT_READER_CONTRACT = "chatgpt-owner-snapshot/v1"
CHATGPT_SOURCE_ID = "chatgpt-owner-snapshot"
SourceId = Literal["imessage", "chatgpt-owner-snapshot"]
ReaderContract = Literal["imessage-owner-snapshot/v1", "chatgpt-owner-snapshot/v1"]
# One reader contract is one lane: its fixed source id and the exact sentence the owner attests.
LANE_SOURCE = {IMESSAGE_READER_CONTRACT: "imessage", CHATGPT_READER_CONTRACT: CHATGPT_SOURCE_ID}
LANE_ATTESTATION = {IMESSAGE_READER_CONTRACT: OWNER_ATTESTATION, CHATGPT_READER_CONTRACT: CHATGPT_OWNER_ATTESTATION}


class _SelectsReader(StrictModel):
    """Describe and enroll name their reader contract; iMessage is the implicit default.

    The default never travels: an older node's closed model refuses the key, and
    both ends sign ``model_dump()``, so an explicit default would also break the
    signature. It is therefore refused on input rather than normalized away.
    """

    @model_validator(mode="before")
    @classmethod
    def _implicit_default(cls, raw):
        if isinstance(raw, dict) and raw.get("reader_contract") == IMESSAGE_READER_CONTRACT:
            raise ValueError("default reader contract is implicit")
        return raw

    @model_validator(mode="after")
    def _one_lane(self):
        if LANE_SOURCE[self.reader_contract] != self.source_id:
            raise ValueError("reader contract and source")
        return self

    @model_serializer(mode="wrap")
    def _wire(self, handler):
        data = handler(self)
        if self.reader_contract == IMESSAGE_READER_CONTRACT:
            data.pop("reader_contract", None)
        return data


class DescribeSnapshot(_SelectsReader):
    operation: Literal["describe"] = "describe"
    source_id: SourceId = "imessage"
    snapshot_id: Identifier
    reader_contract: ReaderContract = IMESSAGE_READER_CONTRACT


class EnrollSnapshot(_SelectsReader):
    operation: Literal["enroll"] = "enroll"
    source_id: SourceId = "imessage"
    snapshot_id: Identifier
    snapshot_sha256: Hash
    dataset_id: Identifier
    owner_attestation: Literal["I attest that this snapshot contains my iMessage account and that native sent-by-me messages are mine.",
                               "I attest that this ChatGPT export contains only my own ChatGPT account and that the prompts it records as mine were written by me."]
    reader_contract: ReaderContract = IMESSAGE_READER_CONTRACT

    @model_validator(mode="after")
    def _lane_attestation(self):
        if LANE_ATTESTATION[self.reader_contract] != self.owner_attestation:
            raise ValueError("attestation for another reader")
        return self


class RevokeSnapshot(StrictModel):
    operation: Literal["revoke"] = "revoke"
    source_id: SourceId = "imessage"
    enrollment_id: Identifier


class EnqueueSnapshot(StrictModel):
    operation: Literal["enqueue"] = "enqueue"
    source_id: SourceId = "imessage"
    enrollment_id: Identifier


class SnapshotJobStatus(StrictModel):
    operation: Literal["status"] = "status"
    source_id: SourceId = "imessage"
    job_id: Identifier


class RunSnapshotJob(StrictModel):
    operation: Literal["run"] = "run"
    source_id: SourceId = "imessage"
    job_id: Identifier


IngestRequest = Annotated[DescribeSnapshot | EnrollSnapshot | RevokeSnapshot | EnqueueSnapshot | SnapshotJobStatus | RunSnapshotJob, Field(discriminator="operation")]
REQUEST_MODELS = {model.model_fields["operation"].default: model for model in
                  (DescribeSnapshot, EnrollSnapshot, RevokeSnapshot, EnqueueSnapshot, SnapshotJobStatus, RunSnapshotJob)}


class SnapshotMetadata(StrictModel):
    snapshot_id: Identifier
    snapshot_sha256: Hash
    snapshot_bytes: Number
    reader_contract: ReaderContract
    ownership_basis: Literal["owner_attested_snapshot"]


class EnrollmentMetadata(StrictModel):
    enrollment_id: Identifier
    dataset_id: Identifier
    source_id: SourceId
    revision: Generation
    state: Literal["active", "revoked"]
    ownership_basis: Literal["owner_attested_snapshot"]


class SnapshotRunSuccess(StrictModel):
    status: Literal["ok"]
    messages_created: Number
    conversations_created: Number
    messages_processed: Number
    historical_skipped: Number


class SnapshotRunFailure(StrictModel):
    status: Literal["error"]
    reason_code: Identifier


RunResult = Annotated[SnapshotRunSuccess | SnapshotRunFailure, Field(discriminator="status")]


class SnapshotJobMetadata(StrictModel):
    job_id: Identifier
    enrollment_id: Identifier
    status: Literal["queued", "running", "done", "failed"]
    result: RunResult | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.status in {"queued", "running"} and self.result is not None:
            raise ValueError("unfinished job result")
        if self.status == "done" and not isinstance(self.result, SnapshotRunSuccess):
            raise ValueError("completed job result")
        if self.status == "failed" and not isinstance(self.result, SnapshotRunFailure):
            raise ValueError("failed job result")
        return self


class IngestCommandBody(StrictModel):
    version: Literal["topos-owner-ingest-command/v2"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    command_id: Identifier
    binding: NodeIdentity
    owner_authorization: OwnerAuthorization
    request: IngestRequest
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.audience_id != self.binding.node_id or self.owner_authorization.actor_id != self.binding.owner_id:
            raise ValueError("ingest command binding")
        return self


class SignedIngestCommand(IngestCommandBody):
    signature: Signature


class IngestAckBody(StrictModel):
    version: Literal["topos-owner-ingest-ack/v2"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    command_id: Identifier
    response_to: Hash
    binding: NodeIdentity
    operation: Literal["describe", "enroll", "revoke", "enqueue", "status", "run"]
    result: SnapshotMetadata | EnrollmentMetadata | SnapshotJobMetadata | SnapshotRunSuccess | SnapshotRunFailure | None
    error_code: Identifier | None
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.issuer_id != self.binding.node_id or (self.result is None) == (self.error_code is None):
            raise ValueError("ingest ack binding/result")
        if self.result is not None:
            expected = {"describe": (SnapshotMetadata,), "enroll": (EnrollmentMetadata,),
                        "revoke": (EnrollmentMetadata,), "enqueue": (SnapshotJobMetadata,),
                        "status": (SnapshotJobMetadata,), "run": (SnapshotRunSuccess, SnapshotRunFailure)}
            if not isinstance(self.result, expected[self.operation]):
                raise ValueError("ingest ack operation")
        return self


class SignedIngestAck(IngestAckBody):
    signature: Signature


def command_digest(command):
    return digest(command.model_dump(exclude={"version", "kid", "issued_at", "expires_at", "signature"}))


def sign_ingest_command(body: IngestCommandBody, key) -> SignedIngestCommand:
    return _sign(IngestCommandBody.parse(body.model_dump()), key, SignedIngestCommand)


def verify_ingest_command(raw, *, trusted_keys, issuer_id, identity, frontend_client_id, now):
    command = _verify(SignedIngestCommand.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id,
                      audience_id=identity.node_id, now=now)
    if command.binding.model_dump() != identity.model_dump() or command.owner_authorization.client_id != frontend_client_id:
        raise PolicyError("ingest_command_binding")
    return command


def sign_ingest_ack(body: IngestAckBody, key) -> SignedIngestAck:
    return _sign(IngestAckBody.parse(body.model_dump()), key, SignedIngestAck)


def verify_ingest_ack(raw, *, trusted_keys, issuer_id, audience_id, request, now):
    request = SignedIngestCommand.parse(request.model_dump() if isinstance(request, SignedIngestCommand) else request)
    ack = _verify(SignedIngestAck.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id, audience_id=audience_id, now=now)
    if (ack.binding != request.binding or ack.command_id != request.command_id or
        ack.operation != request.request.operation or ack.response_to != digest(request.model_dump())):
        raise PolicyError("ingest_ack_binding")
    result, query = ack.result, request.request
    if result is not None:
        if isinstance(query, DescribeSnapshot) and (result.snapshot_id, result.reader_contract) != (query.snapshot_id, query.reader_contract):
            raise PolicyError("ingest_ack_target")
        if isinstance(query, EnrollSnapshot) and (result.dataset_id, result.source_id) != (query.dataset_id, query.source_id):
            raise PolicyError("ingest_ack_target")
        if isinstance(query, (RevokeSnapshot, EnqueueSnapshot)) and result.enrollment_id != query.enrollment_id:
            raise PolicyError("ingest_ack_target")
        if isinstance(query, RevokeSnapshot) and result.source_id != query.source_id:
            raise PolicyError("ingest_ack_target")
        if isinstance(query, RevokeSnapshot) and result.state != "revoked":
            raise PolicyError("ingest_ack_target")
        if isinstance(query, SnapshotJobStatus) and result.job_id != query.job_id:
            raise PolicyError("ingest_ack_target")
    return ack
