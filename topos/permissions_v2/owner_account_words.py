"""The share owner's account name words, attested by the control plane for one ask (A2A-4 amendment 10).

Byte-identical in topos/permissions_v2/owner_account_words.py and control_plane/permissions_v2/owner_account_words.py.
It mounts no route, sends nothing and grants no authority: the signed request still has to pass the ledger.

Why: a recipient asks "What has <first name> been working on?". The owner's items are first person and never carry
the owner's own name, so the node must know that the name means the owner. Its own evidence (the attested self
entity) can lack the word, or other entries on the node can carry it too. The control plane knows the account the
share belongs to. It says so here, in a field of its own beside the relay's principal stamp:

    message["owner_account_stamp"] = {"v": 1, "words": [...], "sig": ...}

The words come from the owner's account as the sign-in server holds it (`words_from_account`): never from the
asker's request, whose only field is the question. The signature is the relay stamp key's, over this module's own
domain line and the canonical JSON of the version, the words and the enclosing message's id and type, so a stamp
binds to exactly one ask and reads as no other signed thing (a principal stamp's bytes start with "{", a bind's
with its own line). It carries no times: the message's id is the signed envelope's request id, which the ledger
admits once and inside the envelope's own life.

Both ways compatible. A node from before this module reads only the keys it knows of a relay message and never sees
the field. A control plane from before it sends none, and the node then has no account words: the rule of 1.5.6.
A field that is present and is not exactly this grammar, or does not verify, refuses the ask.
"""
from __future__ import annotations

import base64
import re
import unicodedata

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import PolicyError, canonical_bytes

FIELD = "owner_account_stamp"
VERSION = 1
DOMAIN = b"topos-owner-account-words/v1\n"
#: The one relay message type that may carry the field: the ask. A fetch carries none.
MESSAGE_TYPE = "permissions_v2_answer_submit"
MAX_WORDS = 8
MIN_LETTERS = 2
MAX_LETTERS = 32
INVALID = "owner_account_words_invalid"

_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}")


def _word(value) -> bool:
    """One word of the grammar: letters only, case-folded and NFKC already, MIN_LETTERS to MAX_LETTERS long."""
    return (type(value) is str and MIN_LETTERS <= len(value) <= MAX_LETTERS and value.isalpha()
            and unicodedata.normalize("NFKC", value).casefold() == value)


def _split(text) -> list[str]:
    if not isinstance(text, str):
        return []
    folded = unicodedata.normalize("NFKC", text).casefold()
    return [word for word in _LETTERS.findall(folded) if _word(word)]


def words_from_account(address=None, names=()) -> tuple[str, ...]:
    """The account's name words, in the wire's order (sorted, each once), at most MAX_WORDS; () when there are none.

    `names`: the account's own given and family name, as the sign-in server holds them. `address`: the account's
    verified address; only its local part, up to a "+" tag, is read. Both are split on everything that is not a
    letter; a piece shorter than MIN_LETTERS or longer than MAX_LETTERS is no word. The names' words come first
    when there are more than MAX_WORDS. The caller supplies only what the account itself holds: never anything
    from a request."""
    found: list[str] = []
    for name in names if isinstance(names, (list, tuple)) else ():
        found += _split(name)
    if isinstance(address, str) and address.count("@") == 1:
        found += _split(address.partition("@")[0].partition("+")[0])
    return tuple(sorted(list(dict.fromkeys(found))[:MAX_WORDS]))


def parse_words(raw) -> tuple[str, ...]:
    """Exactly the wire's list: 1 to MAX_WORDS words (`_word`), sorted, each once. Anything else is refused."""
    if (type(raw) not in (list, tuple) or not 1 <= len(raw) <= MAX_WORDS or not all(_word(word) for word in raw)
            or list(raw) != sorted(set(raw))):
        raise PolicyError(INVALID)
    return tuple(raw)


def signing_bytes(words, *, msg_id: str, msg_type: str) -> bytes:
    if type(msg_id) is not str or not msg_id or msg_type != MESSAGE_TYPE:
        raise PolicyError(INVALID)
    return DOMAIN + canonical_bytes({"v": VERSION, "words": list(parse_words(words)), "msg_id": msg_id,
                                     "msg_type": msg_type})


def sign_stamp(words, *, msg_id: str, msg_type: str, signer) -> dict:
    """The field's value. `signer` is anything with `sign(bytes) -> bytes` holding the relay stamp key."""
    signature = signer.sign(signing_bytes(words, msg_id=msg_id, msg_type=msg_type))
    return {"v": VERSION, "words": list(parse_words(words)),
            "sig": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}


def verify_stamp(raw, *, msg_id: str, msg_type: str, key) -> frozenset[str]:
    """The words of a field that is exactly this grammar and verifies under `key` (the pinned relay stamp key, 32
    bytes) for this very message. Raises `PolicyError` otherwise, with no candidate data: the caller refuses."""
    if (type(raw) is not dict or set(raw) != {"v", "words", "sig"} or type(raw["v"]) is not int
            or raw["v"] != VERSION or type(raw["sig"]) is not str or not _SIGNATURE.fullmatch(raw["sig"])
            or not isinstance(key, bytes) or len(key) != 32):
        raise PolicyError(INVALID)
    signed = signing_bytes(raw["words"], msg_id=msg_id, msg_type=msg_type)
    try:
        signature = base64.urlsafe_b64decode(raw["sig"] + "==")
        if base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=") != raw["sig"]:
            raise ValueError("signature encoding")
        Ed25519PublicKey.from_public_bytes(key).verify(signature, signed)
    except (ValueError, InvalidSignature):
        raise PolicyError(INVALID) from None
    return frozenset(raw["words"])
