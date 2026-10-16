"""What a log line may say about a frame, a body or a text it must not quote: its size and a short digest (NR181).

A relay frame can carry a recipient's question, an answer or shared records, and the owner's own messages travel
the same way. A line that has to point at one (it did not parse, its handler failed) names it by its length and the
first characters of its SHA-256, never by any of its content. The length is of the bytes as they travelled (UTF-8
for text). The digest is plain SHA-256 so the same frame can be matched across the node's and the control plane's
logs; a frame carries request ids, envelopes and signatures, so it cannot be guessed whole and confirmed by digest.
"""
from __future__ import annotations

import hashlib
import json

#: Hex characters of the digest a line carries (48 bits): enough to tell frames apart in a log, not to search for one.
DIGEST_HEX = 12


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
    """`length=<bytes> sha256=<first DIGEST_HEX hex>` of `value`; nothing of the value itself. Never raises."""
    try:
        data = _as_bytes(value)
    except Exception:  # noqa: BLE001 -- a value that cannot be read is still named, by its type only
        return f"length=? type={type(value).__name__}"
    return f"length={len(data)} sha256={hashlib.sha256(data).hexdigest()[:DIGEST_HEX]}"
