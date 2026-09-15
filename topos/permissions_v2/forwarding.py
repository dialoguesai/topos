"""Strict node disclosure proof, separate from policy mutation and grantee proof.

This shared module mounts no route and grants no authority. Output is transported
separately and must match the exact closed MessageDisclosure schema and hash.
"""
from __future__ import annotations

import base64
from typing import Annotated, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import StringConstraints, model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Hash, Identifier, MessageDisclosure, Number, StrictModel
from .signing import AuthorityBinding, MAX_TTL_SECONDS, SignedEnvelope

DOMAIN = b"topos-node-disclosure/v1\n"


class ReleaseBody(StrictModel):
    version: Literal["topos-node-disclosure/v1"]
    kid: Identifier
    envelope_hash: Hash
    request_id: Identifier
    request_hash: Hash
    authority: AuthorityBinding
    output_hash: Hash
    checked_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def lifetime(self):
        if not 0 < self.expires_at - self.checked_at <= MAX_TTL_SECONDS:
            raise ValueError("release lifetime")
        return self


class SignedNodeResult(ReleaseBody):
    signature: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]


def node_result_signing_bytes(body: ReleaseBody) -> bytes:
    parsed = ReleaseBody.parse(body.model_dump(exclude={"signature"}))
    return DOMAIN + canonical_bytes(parsed.model_dump())


def sign_node_result(body: ReleaseBody, key: Ed25519PrivateKey) -> SignedNodeResult:
    body = ReleaseBody.parse(body.model_dump())
    signature = base64.urlsafe_b64encode(key.sign(node_result_signing_bytes(body))).decode("ascii").rstrip("=")
    return SignedNodeResult.parse({**body.model_dump(), "signature": signature})


def verify_node_result(raw, *, trusted_keys: Mapping[str, bytes], envelope: SignedEnvelope,
                       output: MessageDisclosure | dict, now: int) -> SignedNodeResult:
    """Current keys and exact issuance required; signature alone is not a permit."""
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")
    envelope = SignedEnvelope.parse(envelope.model_dump())
    output = MessageDisclosure.parse(output.model_dump() if isinstance(output, MessageDisclosure) else output)
    result = SignedNodeResult.parse(raw.model_dump() if isinstance(raw, SignedNodeResult) else raw)
    authority = AuthorityBinding.parse({field: getattr(envelope, field) for field in AuthorityBinding.model_fields})
    if (result.envelope_hash != digest(envelope.model_dump()) or result.request_id != envelope.request_id
        or result.request_hash != envelope.request_hash or result.authority != authority
        or result.output_hash != digest(output.model_dump())):
        raise PolicyError("result_binding")
    if (result.checked_at > now or result.checked_at < envelope.issued_at or result.expires_at <= now
        or result.expires_at > envelope.expires_at or envelope.expires_at <= now):
        raise PolicyError("result_time")
    key = trusted_keys.get(result.kid)
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("signing_key_unknown")
    try:
        signature = base64.urlsafe_b64decode(result.signature + "==")
        if base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != result.signature:
            raise ValueError("signature encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(signature, node_result_signing_bytes(result))
    except (ValueError, InvalidSignature):
        raise PolicyError("signature_invalid") from None
    return result
