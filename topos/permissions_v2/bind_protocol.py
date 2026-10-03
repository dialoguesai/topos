"""Binding a node for sharing (contract A2A-1): the control plane's signed bind and the node's signed proof.

Byte-identical in topos/permissions_v2/bind_protocol.py and control_plane/permissions_v2/bind_protocol.py.
It mounts no route, sends nothing and grants no authority.

The bind is signed with the control plane's relay stamp key. Before a node is bound that is the only
control-plane key it trusts: topos/relay_stamp.py pinned it at first boot. The proof is signed with
the node key the bind made, or with the key the node already had. The control plane learns that key
from the proof, and the signature shows the node holds it.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import StringConstraints, model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Hash, Identifier, Number, StrictModel

MESSAGE_TYPE = "permissions_v2_bind"
BIND_VERSION = "topos-node-bind/v1"
PROOF_VERSION = "topos-node-bind-proof/v1"
#: In the node's heartbeat capabilities: 1 means this module's bind is handled; 0 or absent, it is not.
CAPABILITY_FIELD = "permissions_v2_bind_version"
CAPABILITY_VERSION = 1
#: In the node's heartbeat capabilities: the bound node's current key id, or None when it is not bound.
KEY_ID_FIELD = "permissions_v2_node_key_id"
ENVIRONMENT_PREFIX = "permissions-beta-"
MAX_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 60

Signature = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]
EngineVersion = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")]


def node_key_id(public_key: bytes) -> str:
    """The key id of a node key a bind made: derived from the key, so a new key always has a new id."""
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise PolicyError("node_key_invalid")
    return "nk_" + hashlib.sha256(public_key).hexdigest()[:32]


def mint_node_id() -> str:
    """A node id for a first bind: random, never derived from a key or from the engine key."""
    return "node_" + secrets.token_hex(16)


class BindBody(StrictModel):
    version: Literal["topos-node-bind/v1"]
    request_id: Identifier
    environment_id: Identifier
    resource_id: Identifier
    owner_id: Identifier
    node_id: Identifier | None
    new_key_allowed: bool
    cp_issuer_id: Identifier
    trusted_cp_keys: dict[Identifier, Hash]
    frontend_client_id: Identifier
    nonce: Hash
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if not self.environment_id.startswith(ENVIRONMENT_PREFIX):
            raise ValueError("bind environment")
        if len(self.trusted_cp_keys) != 1:
            raise ValueError("bind control-plane keys")
        if self.node_id is None and not self.new_key_allowed:
            raise ValueError("a first bind makes a key")
        if not 0 < self.expires_at - self.issued_at <= MAX_TTL_SECONDS:
            raise ValueError("bind lifetime")
        return self


class SignedBind(BindBody):
    signature: Signature


class BindProofBody(StrictModel):
    version: Literal["topos-node-bind-proof/v1"]
    kid: Identifier
    issuer_id: Identifier
    audience_id: Identifier
    request_id: Identifier
    bind_hash: Hash
    nonce: Hash
    environment_id: Identifier
    resource_id: Identifier
    owner_id: Identifier
    node_id: Identifier
    node_public_key: Hash
    outcome: Literal["bound", "already_bound"]
    engine_version: EngineVersion
    issued_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def coherent(self):
        if self.issuer_id != self.node_id:
            raise ValueError("proof issuer")
        if self.outcome == "bound" and self.kid != node_key_id(bytes.fromhex(self.node_public_key)):
            raise ValueError("proof key id")
        if not 0 < self.expires_at - self.issued_at <= MAX_TTL_SECONDS:
            raise ValueError("proof lifetime")
        return self


class SignedBindProof(BindProofBody):
    signature: Signature


def signing_bytes(body: BindBody | BindProofBody) -> bytes:
    """The exact bytes signed: the version line, then Topos canonical JSON v1 of every field but the signature."""
    return (body.version + "\n").encode("ascii") + canonical_bytes(body.model_dump(exclude={"signature"}))


def _encode(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    raw = base64.urlsafe_b64decode(value + "==")
    if _encode(raw) != value:
        raise ValueError("signature encoding")
    return raw


def _clock(now: int) -> None:
    if type(now) is not int or now < 0:
        raise PolicyError("clock_invalid")


def bind_hash(bind: SignedBind) -> str:
    """sha256 of the canonical JSON of the whole signed bind, its signature included."""
    return digest(SignedBind.parse(bind.model_dump()).model_dump())


def sign_bind(body: BindBody, stamp_key: Ed25519PrivateKey) -> SignedBind:
    """Control plane: sign a bind with the relay stamp key."""
    body = BindBody.parse(body.model_dump())
    return SignedBind.parse({**body.model_dump(), "signature": _encode(stamp_key.sign(signing_bytes(body)))})


def verify_bind(raw, *, stamp_public_key: bytes, message_id: str, now: int) -> SignedBind:
    """Node: the bind's shape, the frame it rode in, its time, and the pinned stamp key's signature."""
    _clock(now)
    bind = SignedBind.parse(raw)
    if bind.request_id != message_id:
        raise PolicyError("bind_frame_mismatch")
    if bind.issued_at > now + CLOCK_SKEW_SECONDS or bind.expires_at <= now:
        raise PolicyError("bind_expired")
    if not isinstance(stamp_public_key, bytes) or len(stamp_public_key) != 32:
        raise PolicyError("stamp_key_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(stamp_public_key).verify(_decode(bind.signature), signing_bytes(bind))
    except (ValueError, InvalidSignature):
        raise PolicyError("bind_signature_invalid") from None
    return bind


def sign_bind_proof(*, bind: SignedBind, node_id: str, node_key: Ed25519PrivateKey, kid: str,
                    outcome: str, engine_version: str, now: int) -> SignedBindProof:
    """Node: answer exactly this bind with the node key, binding everything the bind said."""
    _clock(now)
    body = BindProofBody.parse({
        "version": PROOF_VERSION, "kid": kid, "issuer_id": node_id, "audience_id": bind.cp_issuer_id,
        "request_id": bind.request_id, "bind_hash": bind_hash(bind), "nonce": bind.nonce,
        "environment_id": bind.environment_id, "resource_id": bind.resource_id, "owner_id": bind.owner_id,
        "node_id": node_id, "node_public_key": node_key.public_key().public_bytes_raw().hex(),
        "outcome": outcome, "engine_version": engine_version,
        "issued_at": now, "expires_at": now + MAX_TTL_SECONDS})
    return SignedBindProof.parse({**body.model_dump(), "signature": _encode(node_key.sign(signing_bytes(body)))})


def verify_bind_proof(raw, *, bind: SignedBind, now: int) -> SignedBindProof:
    """Control plane: the node's answer to exactly `bind`, signed by the key it names."""
    _clock(now)
    proof = SignedBindProof.parse(raw)
    if proof.issued_at > now + CLOCK_SKEW_SECONDS or proof.expires_at <= now:
        raise PolicyError("proof_expired")
    if (proof.audience_id != bind.cp_issuer_id or proof.request_id != bind.request_id
            or proof.nonce != bind.nonce or proof.bind_hash != bind_hash(bind)
            or proof.environment_id != bind.environment_id or proof.resource_id != bind.resource_id
            or proof.owner_id != bind.owner_id
            or (bind.node_id is not None and proof.node_id != bind.node_id)):
        raise PolicyError("proof_binding")
    if proof.outcome == "bound" and not bind.new_key_allowed:
        raise PolicyError("proof_new_key_not_allowed")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(proof.node_public_key)).verify(
            _decode(proof.signature), signing_bytes(proof))
    except (ValueError, InvalidSignature):
        raise PolicyError("proof_signature_invalid") from None
    return proof
