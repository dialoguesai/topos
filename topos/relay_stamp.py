"""Signed relay principal stamps — principal fabric P3 (engine half).

The CP classifies its callers at its own door; until now that classification
died at the relay and the engine fell back to forwarded-id equality plus the
spoofable X-Topos-Client heuristic. A stamp carries the classification across:

    message["principal_stamp"] = {v, cls, client_id, acting_user, iat, exp, sig}

with `sig` an Ed25519 signature over the canonical JSON of the stamp fields
PLUS the enclosing message's id and type — binding each stamp to exactly one
message, so a captured stamp cannot be replayed onto a different request.

Channel-bound by construction: only the relay dispatch path calls the verifier,
so a stamp arriving over local HTTP is dead weight nobody parses. Never to
owner: a missing, malformed, expired, or unverifiable stamp resolves to None.
What None means is the caller's to say, and since 1.5.0 (review S4, M1) the
relay dispatcher tells two cases apart (core/handlers dispatch_relay_message):
a message with NO stamp keeps the CP_RELAY deferral (forwarded-id equality +
the CP-side containment), and a message whose stamp is there but did not verify
is the least class there is, refused for everything but the share doors, which
refuse it themselves. Before that both were the deferral, a class above the
third party the stamp may have named. The stamp can only ever NARROW OR NAME,
with one exception guarded by the allowlist below: it can mint owner_app for
the owner's native surfaces — which is why the verifying key must be the CP's,
pinned, and never taken from the message itself.

Key pinning (P3.1): env TOPOS_CP_STAMP_PUBKEY (base64, 32 raw bytes) wins;
else the pinned file ~/.topos/cp_stamp_key.pub (same encoding); else no stamp
verifies, and a stamped message is refused like any other that does not
verify. Distribution of the key at pairing is the P3.2 wiring. A value that
does not decode to 32 bytes is no key (review R1 node, R-M3): an empty or cut
pin file used to count as a key for ever, which nothing could verify under.

Times (review R1 node, R-M2; the owner's decision of 7 Oct 2026): a stamp is
accepted up to SKEW_S either side of its own life, ``iat - SKEW_S <= now <=
exp + SKEW_S``. Since a stamp that does not verify is refused, the window
decides whether the owner's own app works on a computer whose clock is off;
it was 60 s one way and the stamp's 120 s life the other. The signature is
checked before the times, so a wrong clock (the control plane's stamp, the
times off) is told from a wrong key, and ``check_relay_stamp`` says which.

The file is pinned once, trust on first use, by a thread the app starts
(``start_first_pin``). While the node holds no key that thread tries again,
backed off; once a key is pinned, by it or by anyone, it ends for good. A key
that is pinned is never asked for again, replaced or overwritten.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .principal import OWNER_APP, THIRD_PARTY, Principal

logger = logging.getLogger("topos.relay_stamp")

STAMP_FIELD = "principal_stamp"
#: Classes a stamp may mint. `owner_automation` reserved for the routine lane.
ALLOWED_CLASSES = frozenset({OWNER_APP, THIRD_PARTY, "owner_automation"})
#: Hard cap on stamp lifetime; anything longer is treated as invalid.
MAX_LIFETIME_S = 600
#: How far this node's clock may be from the control plane's, either way: a stamp is accepted from SKEW_S before it
#: was issued to SKEW_S after it expired. The signature and the binding to one frame hold throughout.
SKEW_S = 300
#: An Ed25519 key and signature, in bytes.
_KEY_BYTES, _SIGNATURE_BYTES = 32, 64

#: Why a stamp did not verify (``check_relay_stamp``): no stamp field at all; no key pinned on this node; not a
#: stamp this node can read (not an object, no usable signature or times, a life over the cap); a signature that
#: does not verify under the pinned key (another key, or a stamp changed or lifted from another frame); a class this
#: node does not know; times outside the window, on a stamp that is the control plane's.
NO_STAMP, NO_KEY, MALFORMED, WRONG_KEY, UNKNOWN_CLASS, WRONG_CLOCK = (
    "no_stamp", "no_key", "malformed", "key", "class", "clock")
VERIFIED = "verified"

_PINNED_KEY_PATH = "~/.topos/cp_stamp_key.pub"
_ENV_KEY = "TOPOS_CP_STAMP_PUBKEY"

#: The first pin is tried again while the node holds no key: after this many seconds, then twice as long each time,
#: and never more than FIRST_PIN_RETRY_MAX_S apart. Each try is one request with a 10 s limit.
FIRST_PIN_RETRY_FIRST_S = 5.0
FIRST_PIN_RETRY_MAX_S = 600.0
#: The thread that pins the first key (``start_first_pin``).
FIRST_PIN_THREAD = "topos-stamp-autopin"


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


def _key_from(text: str) -> Optional[bytes]:
    """The 32 bytes a pinned value holds, or None: not base64, or not a key's length (R-M3)."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except Exception:  # noqa: BLE001
        return None
    return raw if len(raw) == _KEY_BYTES else None


def _load_public_key_bytes() -> Optional[bytes]:
    """The pinned stamp key, or None when this node holds none. A value that is not 32 bytes is none: until 1.5.0
    anything that decoded counted, an empty pin file included, and the node then held "a key" under which nothing
    verified and never asked for another."""
    raw = (os.environ.get("TOPOS_CP_STAMP_PUBKEY") or "").strip()
    if raw:
        key = _key_from(raw)
        if key is None:
            logger.warning("TOPOS_CP_STAMP_PUBKEY is not a 32-byte base64 key; stamps do not verify")
        return key
    path = Path(os.path.expanduser(_PINNED_KEY_PATH))
    if path.is_file():
        try:
            key = _key_from(path.read_text())
        except Exception:  # noqa: BLE001
            key = None
        if key is None:
            logger.warning("the pinned stamp key file does not hold a 32-byte key; this node holds no key")
        return key
    return None


def _now() -> float:
    """This node's clock (one place, so a test can set it without touching the process's)."""
    return time.time()


def check_relay_stamp(message: Dict[str, Any]) -> Tuple[Optional[Principal], str, Optional[float]]:
    """(principal, reason, offset): the stamp's Principal when it verifies, else None and why it did not.

    The signature is checked BEFORE the times (review R1 node, R-M2): a stamp that fails it is a key problem
    (WRONG_KEY), whatever its times say, and only a stamp that the pinned key did sign can be a clock problem
    (WRONG_CLOCK). For that one, ``offset`` is how long ago this node's clock says the stamp was issued, in seconds:
    about how far this node's clock is ahead of the control plane's (negative: behind). None otherwise.
    """
    if STAMP_FIELD not in message:
        return None, NO_STAMP, None
    stamp = message.get(STAMP_FIELD)
    if not isinstance(stamp, dict):
        return None, MALFORMED, None
    key_bytes = _load_public_key_bytes()
    if key_bytes is None:
        return None, NO_KEY, None
    try:
        cls = str(stamp.get("cls") or "")
        iat = float(stamp.get("iat") or 0)
        exp = float(stamp.get("exp") or 0)
        sig = base64.b64decode(str(stamp.get("sig") or ""), validate=True)
        payload = canonical_signing_payload(
            stamp,
            msg_id=str(message.get("id") or ""),
            msg_type=str(message.get("type") or ""),
        )
    except Exception:  # noqa: BLE001 — not a stamp this node can read
        logger.debug("relay stamp rejected", exc_info=True)
        return None, MALFORMED, None
    # A time that is not a finite number passes every comparison below (review R1 node, note N1): never a stamp.
    if len(sig) != _SIGNATURE_BYTES or not exp or not math.isfinite(iat) or not math.isfinite(exp):
        return None, MALFORMED, None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(key_bytes).verify(sig, payload)
    except Exception:  # noqa: BLE001 — any verification trouble is "not verified", never wider
        logger.debug("relay stamp rejected", exc_info=True)
        return None, WRONG_KEY, None
    if cls not in ALLOWED_CLASSES:
        return None, UNKNOWN_CLASS, None
    if exp - iat > MAX_LIFETIME_S:
        return None, MALFORMED, None
    now = _now()
    if not iat - SKEW_S <= now <= exp + SKEW_S:
        return None, WRONG_CLOCK, now - iat
    return Principal(
        cls=cls,
        channel="cp_relay",
        client_id=str(stamp.get("client_id") or ""),
        acting_user=str(stamp.get("acting_user") or ""),
    ), VERIFIED, None


def verify_relay_stamp(message: Dict[str, Any]) -> Optional[Principal]:
    """Resolve a relay message's stamp to a Principal, or None when none verifies.

    None covers both "no stamp" and "a stamp that did not verify"; the relay
    dispatcher tells them apart by the field's presence (review S4, M1) and the
    share doors refuse either. Only a stamp that verifies end to end names a
    class — and then only within ALLOWED_CLASSES. ``check_relay_stamp`` is the
    same check and also says why.
    """
    return check_relay_stamp(message)[0]


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


def _file_holds_a_key(path: Path) -> bool:
    """Whether the pin file holds a key, read on its own: the environment's value is not consulted."""
    try:
        return _key_from(path.read_text()) is not None
    except Exception:  # noqa: BLE001 -- no file, unreadable, or not a key
        return False


def _write_first_pin(path: Path, key_b64: str) -> bool:
    """Put the first pin in place: written whole beside the file, then linked to its name (review R1 node, R-M3).

    The link is atomic and exclusive. A death at any moment leaves either no pin file or a whole one, never the
    empty file a truncate-then-write could leave (which then read as "a key is pinned" for ever). And of two
    writers, the first stands (R-L6a): the link fails when the name is taken. A file that is there and holds no key
    (an empty or cut pin from an older build) is the one thing replaced. True when this call's key is the pin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    beside = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(beside, "w", encoding="utf-8") as handle:
            handle.write(key_b64 + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(beside, path)
        except FileExistsError:
            if _file_holds_a_key(path):
                return False                      # pinned meanwhile, by anyone: never replaced
            os.replace(beside, path)              # not a key: an unusable pin file repairs itself, whole
        return True
    finally:
        try:
            os.unlink(beside)
        except OSError:
            pass


def autopin_stamp_key(stop: Optional[threading.Event] = None) -> bool:
    """P5 convergence: pin the CP's stamp key on first boot, trust-on-first-use.

    Runs only when NO key is pinned anywhere (env or file) — an existing pin is
    never overwritten, so a swapped CP cannot rotate itself into trust; rotation
    is a deliberate owner action (delete the pinned file). TOFU rides the same
    TLS channel the node already trusts for its entire relay, and makes signed
    stamps zero-step for every node — local and hosted alike, which is the
    point: the hosted node's only door is the relay, and this is its key.
    Never raises; a CP without stamping (404), or one that cannot be reached,
    leaves the node with no key, and ``pin_first_key`` asks again later.
    ``stop`` set (the app is shutting down) means a late answer pins nothing.
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
        if _key_from(key_b64) is None:
            return False
        path = Path(os.path.expanduser(_PINNED_KEY_PATH))
        # Asked again just before the write: a key pinned while the answer was on its way, or one in the file
        # behind an environment value this node cannot use, is never replaced. Nor is anything pinned once the
        # app is stopping.
        if _load_public_key_bytes() is not None or _file_holds_a_key(path):
            return False
        if stop is not None and stop.is_set():
            return False
        if not _write_first_pin(path, key_b64):
            return False
        logger.info("pinned CP stamp key from %s (trust-on-first-use)", base)
        return True
    except Exception:  # noqa: BLE001 — pinning is opportunistic, never load-bearing
        logger.debug("stamp key autopin skipped", exc_info=True)
        return False


# --- the first pin, tried again while the node holds no key (review S4 follow-up, Q8) ------------------------------

def pin_first_key(stop: threading.Event) -> bool:
    """The start-up thread's body: pin the control plane's stamp key, trying again while this node holds none.

    The same trust decision as at first boot (``autopin_stamp_key``), repeated, and only ever that one. A stamp the
    node cannot check is refused (review S4, M1), so a node whose one try at start failed (the control plane was
    unreachable) refused every stamped frame until its next restart. It now tries again: after
    FIRST_PIN_RETRY_FIRST_S, then twice as long each time, never more than FIRST_PIN_RETRY_MAX_S apart.

    It ends for good, and asks nobody, as soon as a key is pinned in the file or in the environment, by this thread
    or by anyone. So a pinned key is never asked for again, whatever happens to verification afterwards: asking
    again when stamps stop verifying would let a control plane that was swapped rotate itself into trust, and
    replacing a pin stays the owner's own act (delete the file). An environment value the node cannot use is the
    operator's to fix and ends it too, as does a node with no control plane to ask. ``stop`` (the app shutting
    down) ends the wait at once. True when a key is held at the end."""
    from .config.settings import settings

    delay = FIRST_PIN_RETRY_FIRST_S
    while True:
        if _load_public_key_bytes() is not None:
            return True
        if (os.environ.get(_ENV_KEY) or "").strip():
            return False
        if cp_http_base_from_ws_url(getattr(settings, "topos_control_plane_url", "") or "") is None:
            return False
        if autopin_stamp_key(stop):
            return True
        if stop.wait(delay):
            return False
        delay = min(delay * 2, FIRST_PIN_RETRY_MAX_S)


_first_pin_lock = threading.Lock()
_first_pin_stop: Optional[threading.Event] = None
_first_pin_thread: Optional[threading.Thread] = None


def start_first_pin() -> threading.Thread:
    """Start the thread that pins the first key, once: a slow or down control plane must not delay start-up. A
    second call while it runs returns the same thread."""
    global _first_pin_stop, _first_pin_thread
    with _first_pin_lock:
        if _first_pin_thread is not None and _first_pin_thread.is_alive():
            return _first_pin_thread
        stop = threading.Event()
        thread = threading.Thread(target=pin_first_key, args=(stop,), name=FIRST_PIN_THREAD, daemon=True)
        _first_pin_stop, _first_pin_thread = stop, thread
        thread.start()
        return thread


def stop_first_pin(timeout: float = 0.0) -> None:
    """End the tries (app shutdown). Never blocks unless ``timeout`` says how long to wait for the thread."""
    global _first_pin_stop, _first_pin_thread
    with _first_pin_lock:
        stop, thread = _first_pin_stop, _first_pin_thread
        _first_pin_stop = _first_pin_thread = None
    if stop is not None:
        stop.set()
    if thread is not None and timeout > 0 and thread is not threading.current_thread():
        thread.join(timeout)
