"""Owner identity attestation commands: what the owner said, and about what.

A closed, separately signed channel, like the owner snapshot commands and unlike
anything a recipient can reach. The control plane signs the command, the node
signs the ack, and the command is consumed exactly once. Nothing here decides a
release: an attestation says which entities denote the owner, and every fact
still needs the owner's evidence review, the owner's output review and a grant
of the capability whose contract reads attestations at all.

Ids, flags and digests only. No canonical name, alias, identifier or contact
detail appears in a command, an ack or the ledger, so this channel adds no name
exposure to the relay that carries it.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .canonical import PolicyError, digest
from .contract import Generation, Hash, Identifier, Number, StrictModel
from .protection_clock import ATTESTATION_STATEMENT
from .protocol import NodeIdentity, OwnerAuthorization, Signature, _sign, _verify

# The exact sentence the owner confirms. Changing a word is a new statement
# version, and old entries stay bound to the sentence they were made under.
ATTESTATION_SENTENCE = ("I attest that this entity is me, that facts recorded about it are about me, and that "
                        "I may choose to share them.")


class DescribeIdentity(StrictModel):
    """Read-only. Never registers, never enrolls, never advances the clock."""
    operation: Literal["describe"] = "describe"


class AttestIdentity(StrictModel):
    operation: Literal["attest"] = "attest"
    entity_id: Identifier
    statement_version: Literal["owner-identity-attestation/v1"]
    statement: Literal["I attest that this entity is me, that facts recorded about it are about me, and that "
                       "I may choose to share them."]
    # What the owner had in front of them. If any of it moved between the
    # describe and the confirmation, the attestation is refused rather than
    # applied to something the owner did not see.
    expected_entity_type: Identifier
    expected_is_self: Literal[1]
    expected_contact_id: Identifier | None
    expected_composition_revision: Hash


class RevokeIdentity(StrictModel):
    operation: Literal["revoke"] = "revoke"
    entity_id: Identifier
    entry_id: Identifier


IdentityRequest = Annotated[DescribeIdentity | AttestIdentity | RevokeIdentity, Field(discriminator="operation")]
REQUEST_MODELS = {model.model_fields["operation"].default: model
                  for model in (DescribeIdentity, AttestIdentity, RevokeIdentity)}


class IdentitySubject(StrictModel):
    """One owner spelling, as the owner's own review surface sees it.

    `basis` says why the node knows about it, never who it is. Names live only
    on the node's own surfaces; this channel carries none.
    """
    entity_id: Identifier
    exists: bool
    is_self: bool
    basis: Literal["installed", "self_row", "attested", "merge_neighbor"] | None
    entry_id: Identifier | None
    entry_state: Literal["active", "stale", "revoked"] | None
    entry_reason: Identifier | None
    entity_type: Identifier | None
    contact_id: Identifier | None
    composition_revision: Hash | None
    last_identity_event: Number
    rekeyed_facts: Number


class IdentityState(StrictModel):
    version: Literal["topos-owner-identity-state/v1"]
    contract: Literal["owner_attested_v1"]
    statement_version: Literal["owner-identity-attestation/v1"]
    literal_self_shadowed: bool
    generation: Generation
    subjects: Annotated[list[IdentitySubject], Field(max_length=512)]
    permitted_count: Number
    restricted_count: Number

    @model_validator(mode="after")
    def coherent(self):
        ids = [subject.entity_id for subject in self.subjects]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate identity subject")
        active = sum(1 for subject in self.subjects if subject.entry_state == "active")
        # The literal subject is permitted unless an entity row shadows it, and
        # it is never one of these rows.
        if self.permitted_count != active + (0 if self.literal_self_shadowed else 1):
            raise ValueError("identity permit count")
        if self.restricted_count < self.permitted_count:
            raise ValueError("permit set outside restriction set")
        return self


class IdentityEntryMetadata(StrictModel):
    entity_id: Identifier
    entry_id: Identifier
    state: Literal["active", "revoked"]
    generation: Generation


class IdentityCommandBody(StrictModel):
    version: Literal["topos-owner-identity-command/v1"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    command_id: Identifier
    binding: NodeIdentity
    owner_authorization: OwnerAuthorization
    request: IdentityRequest
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.audience_id != self.binding.node_id or self.owner_authorization.actor_id != self.binding.owner_id:
            raise ValueError("identity command binding")
        return self


class SignedIdentityCommand(IdentityCommandBody):
    signature: Signature


class IdentityAckBody(StrictModel):
    version: Literal["topos-owner-identity-ack/v1"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    command_id: Identifier
    response_to: Hash
    binding: NodeIdentity
    operation: Literal["describe", "attest", "revoke"]
    result: IdentityState | IdentityEntryMetadata | None
    error_code: Identifier | None
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.issuer_id != self.binding.node_id or (self.result is None) == (self.error_code is None):
            raise ValueError("identity ack binding/result")
        if self.result is not None:
            expected = {"describe": IdentityState, "attest": IdentityEntryMetadata,
                        "revoke": IdentityEntryMetadata}[self.operation]
            if not isinstance(self.result, expected):
                raise ValueError("identity ack operation")
        return self


class SignedIdentityAck(IdentityAckBody):
    signature: Signature


def command_digest(command):
    return digest(command.model_dump(exclude={"version", "kid", "issued_at", "expires_at", "signature"}))


def sign_identity_command(body: IdentityCommandBody, key) -> SignedIdentityCommand:
    return _sign(IdentityCommandBody.parse(body.model_dump()), key, SignedIdentityCommand)


def verify_identity_command(raw, *, trusted_keys, issuer_id, identity, frontend_client_id, now):
    command = _verify(SignedIdentityCommand.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id,
                      audience_id=identity.node_id, now=now)
    if (command.binding.model_dump() != identity.model_dump()
        or command.owner_authorization.client_id != frontend_client_id):
        raise PolicyError("identity_command_binding")
    return command


def sign_identity_ack(body: IdentityAckBody, key) -> SignedIdentityAck:
    return _sign(IdentityAckBody.parse(body.model_dump()), key, SignedIdentityAck)


def verify_identity_ack(raw, *, trusted_keys, issuer_id, audience_id, request, now):
    request = SignedIdentityCommand.parse(request.model_dump() if isinstance(request, SignedIdentityCommand) else request)
    ack = _verify(SignedIdentityAck.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id,
                  audience_id=audience_id, now=now)
    if (ack.binding != request.binding or ack.command_id != request.command_id
        or ack.operation != request.request.operation or ack.response_to != digest(request.model_dump())):
        raise PolicyError("identity_ack_binding")
    if isinstance(ack.result, IdentityEntryMetadata) and ack.result.entity_id != request.request.entity_id:
        raise PolicyError("identity_ack_target")
    if ack.operation == "revoke" and ack.result is not None and ack.result.state != "revoked":
        raise PolicyError("identity_ack_target")
    return ack
