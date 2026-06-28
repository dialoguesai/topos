"""Decrypt Usage Inbox payloads on the Topos engine."""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO = b"topos-usage-inbox-v1"
ENV_INBOX_PRIVATE_KEY = "TOPOS_USAGE_INBOX_PRIVATE_KEY_B64"


def _load_private_key() -> Optional[X25519PrivateKey]:
    raw_b64 = str(os.environ.get(ENV_INBOX_PRIVATE_KEY) or "").strip()
    if not raw_b64:
        return None
    raw = base64.b64decode(raw_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("TOPOS_USAGE_INBOX_PRIVATE_KEY_B64 must decode to 32 bytes")
    return X25519PrivateKey.from_private_bytes(raw)


def decrypt_inbox_records_if_needed(records: List[Any]) -> List[Dict[str, Any]]:
    if not records or not isinstance(records[0], dict):
        return records  # type: ignore[return-value]
    wrapper = records[0]
    if not wrapper.get("_inbox_encrypted"):
        return records  # type: ignore[return-value]
    private_key = _load_private_key()
    if private_key is None:
        raise ValueError("encrypted inbox write requires TOPOS_USAGE_INBOX_PRIVATE_KEY_B64")
    ephemeral_pubkey = base64.b64decode(str(wrapper.get("ephemeral_pubkey") or ""))
    nonce = base64.b64decode(str(wrapper.get("nonce") or ""))
    ciphertext = base64.b64decode(str(wrapper.get("ciphertext") or ""))
    sender_public = X25519PublicKey.from_public_bytes(ephemeral_pubkey)
    shared = private_key.exchange(sender_public)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=HKDF_INFO,
    ).derive(shared)
    plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, None)
    decoded = json.loads(plaintext.decode("utf-8"))
    if not isinstance(decoded, list):
        raise ValueError("decrypted inbox payload must be a list")
    return decoded
