"""What a log line may say about a frame, a body or a text it must not quote: its size and a short keyed digest (NR181).

A relay frame can carry a recipient's question, an answer or shared records, and the owner's own messages travel
the same way. A line that has to point at one (it did not parse, its handler failed) names it by its length and the
first characters of an HMAC-SHA256 over it, never by any of its content. The length is of the bytes as they
travelled (UTF-8 for text).

Why keyed (WS0 review, 15 Oct). A plain SHA-256 of a short text lets anyone who reads the log confirm a guess: hash
"ok", a six-digit code or a phone number and compare. The key is random, made once per process at import, held only
in this module's memory, and never logged, returned or written anywhere, so a digest says nothing to a reader of the
log. Within one run of the node the same text gives the same digest, so repeats can still be told apart; across runs,
and against the control plane's logs, digests do not match, by design.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

#: Hex characters of the digest a line carries (48 bits): enough to tell frames apart in a log, not to search for one.
DIGEST_HEX = 12

# This process's key. Never logged, returned, persisted or derived from anything on disk.
_KEY = secrets.token_bytes(32)


def _as_bytes(value) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", "surrogatepass")
    if value is None:
        return b""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr).encode("utf-8", "surrogatepass")
        except (TypeError, ValueError):
            pass
    return repr(value).encode("utf-8", "surrogatepass")


def fingerprint(value) -> str:
    """`length=<bytes> hmac=<first DIGEST_HEX hex>` of `value`, keyed per process; nothing of the value itself.
    Never raises."""
    try:
        data = _as_bytes(value)
    except Exception:  # noqa: BLE001 -- a value that cannot be read is still named, by its type only
        return f"length=? type={type(value).__name__}"
    return f"length={len(data)} hmac={hmac.new(_KEY, data, hashlib.sha256).hexdigest()[:DIGEST_HEX]}"
