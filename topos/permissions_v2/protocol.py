"""Closed policy mutation/status/ACK wire contract, separate from grantee proof.

This module mounts no routes. Key maps and both endpoint identities come from
trusted pairing configuration, never request fields. An ACK binds one signed
attempt and reports historical application separately from current authority.
"""
from __future__ import annotations

import base64
from typing import Annotated, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import StringConstraints, model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Binding, Hash, Identifier, Number, StrictModel
from .registry import Policy
from .signing import AnyAuthorityBinding, MAX_TTL_SECONDS

Signature = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]


class NodeIdentity(StrictModel):
    environment_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    owner_id: Identifier


class OwnerAuthorization(StrictModel):
    actor_id: Identifier
    client_id: Identifier


class MutationBody(StrictModel):
    version: Literal["topos-policy-mutation/v2"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    command_id: Identifier
    operation: Literal["activate", "revoke"]
    expected_epoch: Number
    authority: AnyAuthorityBinding
    policy: Policy | None
    owner_authorization: OwnerAuthorization
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.authority.node_epoch != self.expected_epoch + 1 or self.audience_id != self.authority.node_id:
            raise ValueError("mutation epoch/audience")
        if self.owner_authorization.actor_id != self.authority.owner_id:
            raise ValueError("mutation owner")
        if self.operation == "activate":
            if self.policy is None or self.policy.binding != binding_of(self.authority) or self.policy.policy_version_id != self.authority.policy_version_id or digest(self.policy.model_dump()) != self.authority.policy_hash or self.policy.versions.capability != self.authority.capability_version:
                raise ValueError("mutation policy")
        elif self.policy is not None:
            raise ValueError("revocation policy")
        return self


class SignedMutation(MutationBody):
    signature: Signature


class StatusRequestBody(StrictModel):
    version: Literal["topos-policy-status-request/v2"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    request_id: Identifier
    binding: Binding
    command_id: Identifier | None
    command_hash: Hash | None
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if (self.command_id is None) != (self.command_hash is None) or self.audience_id != self.binding.node_id:
            raise ValueError("status binding")
        return self


class SignedStatusRequest(StatusRequestBody):
    signature: Signature


class AppliedCommandReceipt(StrictModel):
    command_id: Identifier
    command_hash: Hash
    operation: Literal["activate", "revoke"]
    authority: AnyAuthorityBinding
    applied_at: Number


class NodeGrantState(StrictModel):
    identity: NodeIdentity
    node_epoch: Number
    protection_revision: Hash
    grant_state: Literal["active", "revoked", "absent"]
    authority: AnyAuthorityBinding | None

    @model_validator(mode="after")
    def coherent(self):
        if self.grant_state == "absent":
            if self.authority is not None:
                raise ValueError("absent authority")
        elif self.authority is None:
            raise ValueError("missing authority")
        if self.authority is not None and (
            self.authority.node_epoch != self.node_epoch
            or self.authority.protection_revision != self.protection_revision
            or any(getattr(self.authority, key) != value for key, value in self.identity.model_dump().items())
        ):
            raise ValueError("state binding")
        return self


class AckBody(StrictModel):
    version: Literal["topos-policy-ack/v2"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    response_to: Hash
    request_kind: Literal["mutation", "status"]
    request_id: Identifier
    command_id: Identifier | None
    command_hash: Hash | None
    outcome: Literal["applied", "already_applied", "rejected", "status"]
    reason_code: Literal["ok", "epoch_conflict", "generation_stale", "binding_conflict", "immutable_policy", "policy_time", "protection_changed", "command_conflict", "command_unknown", "command_invalid"]
    receipt: AppliedCommandReceipt | None
    state: NodeGrantState
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if (self.command_id is None) != (self.command_hash is None) or self.issuer_id != self.state.identity.node_id:
            raise ValueError("ack binding")
        if self.receipt is not None and (self.receipt.command_id != self.command_id or self.receipt.command_hash != self.command_hash):
            raise ValueError("ack receipt")
        if self.receipt is not None and (self.receipt.authority.node_epoch > self.state.node_epoch or self.receipt.applied_at > self.issued_at or self.state.grant_state == "absent"):
            raise ValueError("ack chronology")
        if self.outcome == "rejected" and (self.receipt is not None or self.reason_code == "ok"):
            raise ValueError("rejected receipt")
        if self.outcome in {"applied", "already_applied"} and (self.receipt is None or self.reason_code != "ok" or self.request_kind != "mutation"):
            raise ValueError("ack outcome")
        if self.request_kind == "mutation" and (self.command_id is None or self.request_id != self.command_id or self.outcome == "status"):
            raise ValueError("mutation ack")
        if self.request_kind == "status" and self.outcome != "status":
            raise ValueError("status ack")
        if self.outcome == "applied" and (self.receipt.authority != self.state.authority or self.state.grant_state != ("active" if self.receipt.operation == "activate" else "revoked")):
            raise ValueError("applied state")
        return self


class SignedAck(AckBody):
    signature: Signature


def binding_of(authority: AnyAuthorityBinding) -> Binding:
    return Binding.parse({key: getattr(authority, key) for key in Binding.model_fields})


def command_digest(command: MutationBody) -> str:
    """Stable operation identity; retry signing keys and timestamps may change."""
    return digest(command.model_dump(exclude={"version", "kid", "issued_at", "expires_at", "signature"}))


def protocol_signing_bytes(body: MutationBody | StatusRequestBody | AckBody) -> bytes:
    return (body.version + "\n").encode("ascii") + canonical_bytes(body.model_dump(exclude={"signature"}))


def _sign(body, key, signed_type):
    body = type(body).parse(body.model_dump())
    signature = base64.urlsafe_b64encode(key.sign(protocol_signing_bytes(body))).decode("ascii").rstrip("=")
    return signed_type.parse({**body.model_dump(), "signature": signature})


def sign_mutation(body: MutationBody, key: Ed25519PrivateKey) -> SignedMutation:
    return _sign(body, key, SignedMutation)


def sign_status_request(body: StatusRequestBody, key: Ed25519PrivateKey) -> SignedStatusRequest:
    return _sign(body, key, SignedStatusRequest)


def sign_ack(body: AckBody, key: Ed25519PrivateKey) -> SignedAck:
    return _sign(body, key, SignedAck)


def _verify(message, *, trusted_keys: Mapping[str, bytes], issuer_id: str, audience_id: str, now: int):
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")
    if message.issued_at > now or message.expires_at <= now or not 0 < message.expires_at - message.issued_at <= MAX_TTL_SECONDS:
        raise PolicyError("envelope_time")
    if message.issuer_id != issuer_id or message.audience_id != audience_id:
        raise PolicyError("protocol_audience")
    key = trusted_keys.get(message.kid)
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("signing_key_unknown")
    try:
        signature = base64.urlsafe_b64decode(message.signature + "==")
        if base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != message.signature:
            raise ValueError("signature encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(signature, protocol_signing_bytes(message))
    except (ValueError, InvalidSignature):
        raise PolicyError("signature_invalid") from None
    return message


def verify_mutation(raw, *, trusted_keys: Mapping[str, bytes], issuer_id: str, identity: NodeIdentity, frontend_client_id: str, now: int) -> SignedMutation:
    message = _verify(SignedMutation.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id, audience_id=identity.node_id, now=now)
    if any(getattr(message.authority, key) != value for key, value in identity.model_dump().items()) or message.owner_authorization.client_id != frontend_client_id:
        raise PolicyError("mutation_binding")
    return message


def verify_status_request(raw, *, trusted_keys: Mapping[str, bytes], issuer_id: str, identity: NodeIdentity, now: int) -> SignedStatusRequest:
    message = _verify(SignedStatusRequest.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id, audience_id=identity.node_id, now=now)
    if any(getattr(message.binding, key) != value for key, value in identity.model_dump().items()):
        raise PolicyError("status_binding")
    return message


def verify_ack(raw, *, trusted_keys: Mapping[str, bytes], issuer_id: str, audience_id: str, request: SignedMutation | SignedStatusRequest, now: int) -> SignedAck:
    if not isinstance(request, (SignedMutation, SignedStatusRequest)):
        raise PolicyError("request_type")
    request = (SignedMutation if isinstance(request, SignedMutation) else SignedStatusRequest).parse(request.model_dump())
    message = _verify(SignedAck.parse(raw), trusted_keys=trusted_keys, issuer_id=issuer_id, audience_id=audience_id, now=now)
    mutation = isinstance(request, SignedMutation)
    binding = binding_of(request.authority) if mutation else request.binding
    request_id = request.command_id if mutation else request.request_id
    expected_hash = command_digest(request) if mutation else request.command_hash
    if message.response_to != digest(request.model_dump()) or message.request_kind != ("mutation" if mutation else "status") or message.request_id != request_id or message.command_id != request.command_id or message.command_hash != expected_hash:
        raise PolicyError("ack_correlation")
    if any(getattr(binding, key) != value for key, value in message.state.identity.model_dump().items()):
        raise PolicyError("ack_binding")
    for authority in (message.state.authority, message.receipt.authority if message.receipt else None):
        if authority is not None and binding_of(authority) != binding:
            raise PolicyError("ack_binding")
    if mutation and message.receipt is not None and (message.receipt.authority != request.authority or message.receipt.operation != request.operation):
        raise PolicyError("ack_receipt")
    return message
