"""Mandatory v2 grantee signatures. Never maps failures to legacy or owner."""
from __future__ import annotations

import base64
from typing import Any, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import StringConstraints
from typing_extensions import Annotated

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Binding, Generation, Hash, Identifier, Number, StrictModel

DOMAIN = b"topos-grantee-envelope/v2\n"
MAX_TTL_SECONDS = 120
RequestType = Literal["permissions.v2.preview", "permissions.v2.read"]


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
    signature: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]


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


def request_digest(request_type: RequestType, payload: Any) -> str:
    return digest({"request_type": request_type, "payload": payload})


def signing_bytes(body: EnvelopeBody) -> bytes:
    # Re-validate even model instances: nested Python containers are mutable.
    parsed = EnvelopeBody.parse(body.model_dump(exclude={"signature"}))
    return DOMAIN + canonical_bytes(parsed.model_dump())


def sign_envelope(body: EnvelopeBody, key: Ed25519PrivateKey) -> SignedEnvelope:
    signature = base64.urlsafe_b64encode(key.sign(signing_bytes(body))).decode("ascii").rstrip("=")
    return SignedEnvelope.parse({**body.model_dump(), "signature": signature})


def verify_envelope(
    raw: bytes | str | dict,
    *,
    trusted_keys: Mapping[str, bytes],
    expected_authority: AuthorityBinding,
    request: RequestContext,
    payload: Any,
    now: int,
) -> SignedEnvelope:
    envelope = SignedEnvelope.parse(raw)
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


def verify_current_signature(envelope: SignedEnvelope, *, trusted_keys: Mapping[str, bytes], now: int) -> None:
    """Recheck an admitted envelope's key/time at the final private checkpoint.

    Signature validity alone is never authorization; initial admission also
    requires the full verify_envelope context/authority comparison above.
    """
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
