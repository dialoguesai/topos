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


def cp_http_base_from_ws_url(ws_url: str) -> Optional[str]:
    """wss://cp.example/ws/engine -> https://cp.example (http for ws://)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(ws_url or ""))
        if parsed.scheme not in ("ws", "wss") or not parsed.netloc:
            return None
        scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{scheme}://{parsed.netloc}"
    except Exception:  # noqa: BLE001
        return None


def autopin_stamp_key() -> bool:
    """P5 convergence: pin the CP's stamp key on first boot, trust-on-first-use.

    Runs only when NO key is pinned anywhere (env or file) — an existing pin is
    never overwritten, so a swapped CP cannot rotate itself into trust; rotation
    is a deliberate owner action (delete the pinned file). TOFU rides the same
    TLS channel the node already trusts for its entire relay, and makes signed
    stamps zero-step for every node — local and hosted alike, which is the
    point: the hosted node's only door is the relay, and this is its key.
    Never raises; a CP without stamping (404) just leaves legacy behavior.
    """
    if _load_public_key_bytes() is not None:
        return False
    try:
        from .config.settings import settings

        base = cp_http_base_from_ws_url(getattr(settings, "topos_control_plane_url", "") or "")
        if not base:
            return False
        import httpx

        resp = httpx.get(f"{base}/v1/relay/stamp-public-key", timeout=10.0)
        if resp.status_code != 200:
            return False
        data = resp.json()
        key_b64 = str(data.get("public_key_b64") or "").strip()
        if str(data.get("algorithm") or "") != "ed25519" or not key_b64:
            return False
        raw = base64.b64decode(key_b64, validate=True)
        if len(raw) != 32:
            return False
        path = Path(os.path.expanduser(_PINNED_KEY_PATH))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key_b64 + "\n")
        logger.info("pinned CP stamp key from %s (trust-on-first-use)", base)
        return True
    except Exception:  # noqa: BLE001 — pinning is opportunistic, never load-bearing
        logger.debug("stamp key autopin skipped", exc_info=True)
        return False
