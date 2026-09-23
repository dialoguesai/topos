"""Mandatory v2 grantee signatures. Never maps failures to legacy or owner."""
from __future__ import annotations

import base64
from typing import Any, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import StringConstraints
from typing_extensions import Annotated

from .canonical import PolicyError, canonical_bytes, digest, parse_json
from .contract import Binding, Generation, Hash, Identifier, Number, StrictModel
from .fact_contract import FACT_CAPABILITIES, FactCapability

DOMAIN = b"topos-grantee-envelope/v2\n"
MAX_TTL_SECONDS = 120
RequestType = Literal["permissions.v2.preview", "permissions.v2.read"]
FactRequestType = Literal["permissions.v2.fact.read"]
SearchRequestType = Literal["permissions.v2.search"]
Signature = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]


class AuthorityBinding(Binding):
    grant_generation: Generation
    assignment_generation: Generation
    policy_version_id: Identifier
    policy_hash: Hash
    capability_version: Literal["permissions-beta/p2a-v1"]
    protection_revision: Hash
    node_epoch: Generation


class EnvelopeBody(AuthorityBinding):
    version: Literal["topos-grantee-envelope/v2"]
    kid: Identifier
    request_id: Identifier
    request_type: RequestType
    request_hash: Hash
    issued_at: Number
    expires_at: Number


class SignedEnvelope(EnvelopeBody):
    signature: Signature


class FactAuthorityBinding(AuthorityBinding):
    capability_version: FactCapability


class FactEnvelopeBody(EnvelopeBody):
    capability_version: FactCapability
    request_type: FactRequestType


class SignedFactEnvelope(FactEnvelopeBody):
    signature: Signature


class AttestedSourceAuthorityBinding(AuthorityBinding):
    """p2a-v2 authority: a subclass, so the pinned p2a-v1 export does not move."""
    capability_version: Literal["permissions-beta/p2a-v2"]


class AttestedSourceEnvelopeBody(EnvelopeBody):
    capability_version: Literal["permissions-beta/p2a-v2"]


class SignedAttestedSourceEnvelope(AttestedSourceEnvelopeBody):
    signature: Signature


class OpaqueSourceAuthorityBinding(AuthorityBinding):
    """p2a-v3 authority: a subclass, so the pinned p2a-v1 and p2a-v2 exports do not move."""
    capability_version: Literal["permissions-beta/p2a-v3"]


class OpaqueSourceEnvelopeBody(EnvelopeBody):
    capability_version: Literal["permissions-beta/p2a-v3"]


class SignedOpaqueSourceEnvelope(OpaqueSourceEnvelopeBody):
    signature: Signature


class SearchAuthorityBinding(AuthorityBinding):
    """p2c-v1 authority: a subclass, so the pinned p2a exports do not move."""
    capability_version: Literal["permissions-beta/p2c-v1"]


class SearchEnvelopeBody(EnvelopeBody):
    capability_version: Literal["permissions-beta/p2c-v1"]
    request_type: SearchRequestType


class SignedSearchEnvelope(SearchEnvelopeBody):
    signature: Signature


class RequestContext(StrictModel):
    """Actual authenticated caller/transport context, never copied from a token."""

    environment_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    owner_id: Identifier
    actor_id: Identifier
    client_id: Identifier
    grant_id: Identifier
    assignment_id: Identifier
    request_id: Identifier
    request_type: RequestType


class FactRequestContext(RequestContext):
    request_type: FactRequestType


class SearchRequestContext(RequestContext):
    request_type: SearchRequestType


AnyAuthorityBinding = (AuthorityBinding | FactAuthorityBinding | AttestedSourceAuthorityBinding
                       | OpaqueSourceAuthorityBinding | SearchAuthorityBinding)
AnySignedEnvelope = (SignedEnvelope | SignedFactEnvelope | SignedAttestedSourceEnvelope | SignedOpaqueSourceEnvelope
                     | SignedSearchEnvelope)
AnyEnvelopeBody = (EnvelopeBody | FactEnvelopeBody | AttestedSourceEnvelopeBody | OpaqueSourceEnvelopeBody
                   | SearchEnvelopeBody)
AnyRequestContext = RequestContext | FactRequestContext | SearchRequestContext


def _value(raw):
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    raw = parse_json(raw) if isinstance(raw, (str, bytes)) else raw
    if type(raw) is not dict:
        raise PolicyError("unsupported_capability")
    return raw


def parse_authority(raw) -> AnyAuthorityBinding:
    raw = _value(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v1":
        return AuthorityBinding.parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v2":
        return AttestedSourceAuthorityBinding.parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v3":
        return OpaqueSourceAuthorityBinding.parse(raw)
    if raw.get("capability_version") in FACT_CAPABILITIES:
        return FactAuthorityBinding.parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2c-v1":
        return SearchAuthorityBinding.parse(raw)
    raise PolicyError("unsupported_capability")


def parse_envelope(raw, *, signed=True):
    raw = _value(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v1":
        return (SignedEnvelope if signed else EnvelopeBody).parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v2":
        return (SignedAttestedSourceEnvelope if signed else AttestedSourceEnvelopeBody).parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2a-v3":
        return (SignedOpaqueSourceEnvelope if signed else OpaqueSourceEnvelopeBody).parse(raw)
    if raw.get("capability_version") in FACT_CAPABILITIES:
        return (SignedFactEnvelope if signed else FactEnvelopeBody).parse(raw)
    if raw.get("capability_version") == "permissions-beta/p2c-v1":
        return (SignedSearchEnvelope if signed else SearchEnvelopeBody).parse(raw)
    raise PolicyError("unsupported_capability")


def parse_request_context(raw) -> AnyRequestContext:
    raw = _value(raw)
    if raw.get("request_type") == "permissions.v2.fact.read":
        return FactRequestContext.parse(raw)
    if raw.get("request_type") == "permissions.v2.search":
        return SearchRequestContext.parse(raw)
    return RequestContext.parse(raw)


def request_digest(request_type: RequestType | FactRequestType | SearchRequestType, payload: Any) -> str:
    return digest({"request_type": request_type, "payload": payload})


def signing_bytes(body: AnyEnvelopeBody) -> bytes:
    # Re-validate even model instances: nested Python containers are mutable.
    parsed = parse_envelope(body.model_dump(exclude={"signature"}), signed=False)
    return DOMAIN + canonical_bytes(parsed.model_dump())


def sign_envelope(body: AnyEnvelopeBody, key: Ed25519PrivateKey) -> AnySignedEnvelope:
    signature = base64.urlsafe_b64encode(key.sign(signing_bytes(body))).decode("ascii").rstrip("=")
    return parse_envelope({**body.model_dump(), "signature": signature})


def verify_envelope(
    raw: bytes | str | dict,
    *,
    trusted_keys: Mapping[str, bytes],
    expected_authority: AnyAuthorityBinding,
    request: AnyRequestContext,
    payload: Any,
    now: int,
) -> AnySignedEnvelope:
    envelope = parse_envelope(raw)
    expected_authority = parse_authority(expected_authority)
    request = parse_request_context(request)
    verify_current_signature(envelope, trusted_keys=trusted_keys, now=now)
    for field, value in expected_authority.model_dump().items():
        if getattr(envelope, field) != value:
            raise PolicyError("authority_binding")
    for field, value in request.model_dump().items():
        if getattr(envelope, field) != value:
            raise PolicyError("request_binding")
    if envelope.request_hash != request_digest(request.request_type, payload):
        raise PolicyError("request_hash")
    return envelope


def verify_current_signature(envelope: AnySignedEnvelope, *, trusted_keys: Mapping[str, bytes], now: int) -> None:
    """Recheck an admitted envelope's key/time at the final private checkpoint.

    Signature validity alone is never authorization; initial admission also
    requires the full verify_envelope context/authority comparison above.
    """
    envelope = parse_envelope(envelope)
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")
    if envelope.issued_at > now or envelope.expires_at <= now:
        raise PolicyError("envelope_time")
    if not 0 < envelope.expires_at - envelope.issued_at <= MAX_TTL_SECONDS:
        raise PolicyError("envelope_lifetime")
    key = trusted_keys.get(envelope.kid)
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("signing_key_unknown")
    try:
        sig = base64.urlsafe_b64decode(envelope.signature + "==")
        if base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=") != envelope.signature:
            raise PolicyError("signature_encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(sig, signing_bytes(envelope))
    except (ValueError, InvalidSignature) as exc:
        raise PolicyError("signature_invalid") from exc
