"""Signed relay principal stamps — principal fabric P3 (engine half).

The CP classifies its callers at its own door; until now that classification
died at the relay and the engine fell back to forwarded-id equality plus the
spoofable X-Topos-Client heuristic. A stamp carries the classification across:

    message["principal_stamp"] = {v, cls, client_id, acting_user, iat, exp, sig}

with `sig` an Ed25519 signature over the canonical JSON of the stamp fields
PLUS the enclosing message's id and type — binding each stamp to exactly one
message, so a captured stamp cannot be replayed onto a different request.

Channel-bound by construction: only the relay dispatch path calls the verifier,
so a stamp arriving over local HTTP is dead weight nobody parses. Fail-open to
LEGACY, never to owner: a missing, malformed, expired, or unverifiable stamp
resolves to None and the caller keeps today's CP_RELAY deferral (forwarded-id
equality + the CP-side containment). The stamp can only ever NARROW OR NAME,
with one exception guarded by the allowlist below: it can mint owner_app for
the owner's native surfaces — which is why the verifying key must be the CP's,
pinned, and never taken from the message itself.

Key pinning (P3.1): env TOPOS_CP_STAMP_PUBKEY (base64, 32 raw bytes) wins;
else the pinned file ~/.topos/cp_stamp_key.pub (same encoding); else stamps
are ignored entirely. Distribution of the key at pairing is the P3.2 wiring.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .principal import OWNER_APP, THIRD_PARTY, Principal

logger = logging.getLogger("topos.relay_stamp")

STAMP_FIELD = "principal_stamp"
#: Classes a stamp may mint. `owner_automation` reserved for the routine lane.
ALLOWED_CLASSES = frozenset({OWNER_APP, THIRD_PARTY, "owner_automation"})
#: Hard cap on stamp lifetime; anything longer is treated as invalid.
MAX_LIFETIME_S = 600
#: Tolerated clock skew for iat-in-the-future.
SKEW_S = 60

_PINNED_KEY_PATH = "~/.topos/cp_stamp_key.pub"


def canonical_signing_payload(stamp: Dict[str, Any], *, msg_id: str, msg_type: str) -> bytes:
    """The exact bytes both sides sign: stamp fields + the message binding."""
    body = {
        "v": stamp.get("v"),
        "cls": stamp.get("cls"),
        "client_id": stamp.get("client_id"),
        "acting_user": stamp.get("acting_user"),
        "iat": stamp.get("iat"),
        "exp": stamp.get("exp"),
        "msg_id": msg_id,
        "msg_type": msg_type,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_public_key_bytes() -> Optional[bytes]:
    raw = (os.environ.get("TOPOS_CP_STAMP_PUBKEY") or "").strip()
    if raw:
        try:
            return base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001
            logger.warning("TOPOS_CP_STAMP_PUBKEY is not valid base64; stamps ignored")
            return None
    path = Path(os.path.expanduser(_PINNED_KEY_PATH))
    if path.is_file():
        try:
            return base64.b64decode(path.read_text().strip(), validate=True)
        except Exception:  # noqa: BLE001
            logger.warning("pinned stamp key unreadable at %s; stamps ignored", path)
    return None


def verify_relay_stamp(message: Dict[str, Any]) -> Optional[Principal]:
    """Resolve a relay message's stamp to a Principal, or None for legacy.

    Every failure branch is silent-to-legacy by design (migration invariant:
    a node ahead of its CP, or vice versa, keeps working exactly as today).
    Only a stamp that verifies end to end can change behavior — and then only
    within ALLOWED_CLASSES.
    """
    stamp = message.get(STAMP_FIELD)
    if not isinstance(stamp, dict):
        return None
    key_bytes = _load_public_key_bytes()
    if key_bytes is None:
        return None
    try:
        cls = str(stamp.get("cls") or "")
        if cls not in ALLOWED_CLASSES:
            return None
        now = time.time()
        iat = float(stamp.get("iat") or 0)
        exp = float(stamp.get("exp") or 0)
        if not exp or exp <= now or iat > now + SKEW_S or exp - iat > MAX_LIFETIME_S:
            return None
        sig = base64.b64decode(str(stamp.get("sig") or ""), validate=True)
        payload = canonical_signing_payload(
            stamp,
            msg_id=str(message.get("id") or ""),
            msg_type=str(message.get("type") or ""),
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(key_bytes).verify(sig, payload)
    except Exception:  # noqa: BLE001 — any verification trouble is legacy, never wider
        logger.debug("relay stamp rejected", exc_info=True)
        return None
    return Principal(
        cls=cls,
        channel="cp_relay",
        client_id=str(stamp.get("client_id") or ""),
        acting_user=str(stamp.get("acting_user") or ""),
    )
