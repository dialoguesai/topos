"""The keep window of a kept question, attested by the control plane for one ask (kept questions, lane OFF1N).

Byte-identical in topos/permissions_v2/kept_stamp.py and control_plane/permissions_v2/kept_stamp.py.
It mounts no route, sends nothing and grants no authority: the signed request still has to pass the ledger.

Why: the control plane keeps a question asked while the share owner's Topos is offline and delivers it later as a
normal ask, under fresh authority, and its asker may come back for the answer hours or days after that. A node keeps
an ended answer for ten minutes (`answer_release.UNFETCHED_SECONDS`), which is too short. The control plane says that
the ask is a kept one, and until when its asker may come back, in a field of its own beside the relay's principal
stamp:

    message["kept_stamp"] = {"v": 1, "keep_until": <epoch seconds>, "sig": ...}

`keep_until` is when the control plane stops keeping the question (its kept row's expiry); the node keeps the answer
until then, within its own cap (`answer_release.keep_end`). The signature is the relay stamp key's, over this
module's own domain line and the canonical JSON of the version, the window's end and the enclosing message's id and
type, so a stamp binds to exactly one ask and reads as no other signed thing (a principal stamp's bytes start with
"{", an owner account stamp's with its own line). It carries no issue time: the message's id is the signed envelope's
request id, which the ledger admits once and inside the envelope's own life.

Both ways compatible. A node from before this module reads only the keys it knows of a relay message and never sees
the field, so it would answer a kept ask as a live one: the control plane delivers a kept question only to a node
that advertises `permissions_v2_kept_answers` (CAPABILITY_VERSION). A control plane from before it sends none, and
every ask is live. A field that is present and is not exactly this grammar, or does not verify, refuses the ask; so
does a window that ended (`expired`).
"""
from __future__ import annotations

import base64
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import MAX_INTEGER, PolicyError, canonical_bytes

FIELD = "kept_stamp"
VERSION = 1
DOMAIN = b"topos-kept-stamp/v1\n"
#: The one relay message type that may carry the field: the ask. A fetch carries none.
MESSAGE_TYPE = "permissions_v2_answer_submit"
#: How far the node's clock may be from the control plane's, either way (the relay stamp's SKEW_S, pinned by test).
SKEW_SECONDS = 300
#: `permissions_v2_kept_answers`: the node keeps a kept answer for its window, and the fetch of a kept ask it could
#: not serve then answers 429 `answer_deferred`, to be asked again.
CAPABILITY_VERSION = 1
INVALID = "kept_stamp_invalid"
EXPIRED = "kept_stamp_expired"

_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}")


def parse_keep_until(raw) -> int:
    """Exactly the wire's value: whole epoch seconds, 1 to canonical JSON's largest safe integer. Anything else (a
    bool, a float, a string, zero or less) is refused."""
    if type(raw) is not int or not 1 <= raw <= MAX_INTEGER:
        raise PolicyError(INVALID)
    return raw


def signing_bytes(keep_until, *, msg_id: str, msg_type: str) -> bytes:
    if type(msg_id) is not str or not msg_id or msg_type != MESSAGE_TYPE:
        raise PolicyError(INVALID)
    return DOMAIN + canonical_bytes({"v": VERSION, "keep_until": parse_keep_until(keep_until), "msg_id": msg_id,
                                     "msg_type": msg_type})


def sign_stamp(keep_until, *, msg_id: str, msg_type: str, signer) -> dict:
    """The field's value. `signer` is anything with `sign(bytes) -> bytes` holding the relay stamp key."""
    signature = signer.sign(signing_bytes(keep_until, msg_id=msg_id, msg_type=msg_type))
    return {"v": VERSION, "keep_until": parse_keep_until(keep_until),
            "sig": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}


def verify_stamp(raw, *, msg_id: str, msg_type: str, key) -> int:
    """The `keep_until` of a field that is exactly this grammar and verifies under `key` (the pinned relay stamp key,
    32 bytes) for this very message. Raises `PolicyError` otherwise, with no candidate data: the caller refuses."""
    if (type(raw) is not dict or set(raw) != {"v", "keep_until", "sig"} or type(raw["v"]) is not int
            or raw["v"] != VERSION or type(raw["sig"]) is not str or not _SIGNATURE.fullmatch(raw["sig"])
            or not isinstance(key, bytes) or len(key) != 32):
        raise PolicyError(INVALID)
    signed = signing_bytes(raw["keep_until"], msg_id=msg_id, msg_type=msg_type)
    try:
        signature = base64.urlsafe_b64decode(raw["sig"] + "==")
        if base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != raw["sig"]:
            raise ValueError("signature encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(signature, signed)
    except (ValueError, InvalidSignature):
        raise PolicyError(INVALID) from None
    return raw["keep_until"]


def expired(keep_until, *, now: int) -> bool:
    """Whether the window ended more than SKEW_SECONDS before `now` (the node's clock). The control plane delivers a
    kept question only inside its window, so a stamp past it is no kept ask: the node refuses it."""
    return parse_keep_until(keep_until) + SKEW_SECONDS < now
