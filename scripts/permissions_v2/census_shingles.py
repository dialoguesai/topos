"""Hashed word shingles: the text-scan form of a forbidden set, and a scanner with CanaryScanner's interface over it.

PLAN_FORWARD_2026-09-28.md §4.2 (the IF-1 private file's `shingles:{scheme, key_hex, hashes}`) and §4.4 (the passive
scan). The census (WS1) never hands a scorer forbidden TEXT, only hashes of its normalised word runs; the scorer hashes
what the recipient saw the same way and looks for overlap.

The scheme family is the one the recipient harness (WS2, granteeEvalHash.ts) implements, so one set of hashes serves
the harness's in-memory scan of wire bodies and answers and this scorer's scan of the private answers:

    canary-v1/words:<min>[-<max>]/<sha256|hmac-sha256>          default: canary-v1/words:3-8/hmac-sha256

1. normalise: NFKC, `str.casefold()`, every run of characters that are not Unicode letters or numbers (Python's
   `[\\W_]+`) to one space, trimmed. This is `oracles.normalize`, the CanaryScanner normalisation; a test pins the two
   equal, and the harness ports case folding with a generated table (JavaScript has no casefold);
2. shingle: every run of k consecutive words, min <= k <= max, joined by one space;
3. hash: lowercase hex SHA-256 of the shingle's UTF-8 bytes, or HMAC-SHA256 under `key_hex` (16 to 128 bytes, hex).
   The key lives only in the 0600 private file and is deleted with it.

What goes INTO the set is the census builder's choice (WS1 with WS8); `build` is the reference:
- a forbidden text of at least `max` words contributes its `max`-word runs; one of `min` to `max - 1` words contributes
  itself, whole; a shorter one contributes nothing and is counted, because "ok thanks" means nothing on either side;
- a hash that also occurs among any permitted member's `min`..`max`-word runs is dropped and counted: identical text on
  both sides cannot show which side a copy came from (the census's ambiguous rule, §4.2).
A scanner hashes every `min`..`max`-word run of what it scans, so it finds both kinds of entry.

A hit means a whole forbidden run appears in recipient-visible bytes. In a wire body that is strong evidence. In
model-written prose it is a review signal, since a common eight-word phrase can recur by chance, so the p2c report files
text hits for review and never counts one as a hard violation on its own.

Standard library only and one file, so the census builder in the engine repository can vendor it verbatim; the tests
pin its vectors. Nothing here stores, returns or prints text: `build` returns hashes and counts, `scan` classes and
counts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from typing import Iterable, Mapping

FAMILY = "canary-v1"
MIN_WORDS, MAX_WORDS = 3, 8
SCHEME = "%s/words:%d-%d/hmac-sha256" % (FAMILY, MIN_WORDS, MAX_WORDS)
_SCHEME = re.compile(r"canary-v1/words:([0-9]{1,2})(?:-([0-9]{1,2}))?/(sha256|hmac-sha256)")
_KEY = re.compile(r"(?:[0-9a-f]{2}){16,128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """NFKC, casefold, every run of non-alphanumerics (punctuation, whitespace, underscores) to one space."""
    return " ".join(_NON_ALNUM.sub(" ", unicodedata.normalize("NFKC", text).casefold()).split())


class Scheme:
    """A parsed `canary-v1/words:<min>-<max>/<hash>` scheme and its key."""

    def __init__(self, min_words: int, max_words: int, hash_name: str, key: bytes | None):
        if not 1 <= min_words <= max_words <= 32:
            raise ValueError("shingles: need 1 <= min <= max <= 32 words")
        if (hash_name == "hmac-sha256") != (key is not None):
            raise ValueError("shingles: hmac-sha256 needs a key and sha256 takes none")
        self.min, self.max, self.hash_name, self.key = min_words, max_words, hash_name, key

    @property
    def name(self) -> str:
        return "%s/words:%d-%d/%s" % (FAMILY, self.min, self.max, self.hash_name)

    @classmethod
    def parse(cls, block: Mapping) -> "Scheme":
        """From a shingles block: `scheme` as the string form, or the object form {normalization, unit, size,
        min_size, hash, key_hex}; the key from `key_hex` beside it. Raises on any scheme this module does not
        implement, so a caller reports it instead of scanning with the wrong hashes."""
        value, key_hex = block.get("scheme"), block.get("key_hex")
        if isinstance(value, str):
            match = _SCHEME.fullmatch(value)
            if not match:
                raise ValueError("shingles: unsupported scheme")
            low, high, hash_name = int(match.group(1)), int(match.group(2) or match.group(1)), match.group(3)
        elif isinstance(value, Mapping):
            if value.get("normalization") != FAMILY or value.get("unit") != "words":
                raise ValueError("shingles: unsupported scheme")
            high, hash_name = value.get("size"), value.get("hash")
            low = value.get("min_size", high)
            key_hex = value.get("key_hex", key_hex)
            if type(low) is not int or type(high) is not int or hash_name not in ("sha256", "hmac-sha256"):
                raise ValueError("shingles: unsupported scheme")
        else:
            raise ValueError("shingles: unsupported scheme")
        key = None
        if hash_name == "hmac-sha256":
            if not (isinstance(key_hex, str) and _KEY.fullmatch(key_hex)):
                raise ValueError("shingles: hmac-sha256 needs key_hex")
            key = bytes.fromhex(key_hex)
        return cls(low, high, hash_name, key)

    def hash(self, shingle: str) -> str:
        data = shingle.encode("utf-8")
        return hmac.new(self.key, data, hashlib.sha256).hexdigest() if self.key else hashlib.sha256(data).hexdigest()

    def windows(self, text: str) -> dict[str, int]:
        """Every run a scanner looks at, `min` to `max` words long: hash -> run length."""
        words = normalize(text).split()
        out: dict[str, int] = {}
        for size in range(self.min, self.max + 1):
            for start in range(0, len(words) - size + 1):
                out.setdefault(self.hash(" ".join(words[start:start + size])), size)
        return out

    def entries(self, text: str) -> list[str]:
        """The hashes one forbidden text puts into the set (the census selection rule above)."""
        words = normalize(text).split()
        if len(words) >= self.max:
            runs = [" ".join(words[i:i + self.max]) for i in range(len(words) - self.max + 1)]
        elif len(words) >= self.min:
            runs = [" ".join(words)]
        else:
            runs = []
        return [self.hash(run) for run in runs]


def build(forbidden: Iterable[tuple[str, str]], permitted: Iterable[str], *, key: bytes | None,
          min_words: int = MIN_WORDS, max_words: int = MAX_WORDS) -> dict:
    """The census side. `forbidden` yields (text, class); `permitted` yields every permitted member's text. Returns an
    IF-1 `shingles` block: {scheme, key_hex, hashes: [...], classes: {hash: [class, ...]}, counts}. No text."""
    scheme = Scheme(min_words, max_words, "hmac-sha256" if key else "sha256", key)
    member: set[str] = set()
    for text in permitted:
        member.update(scheme.windows(text))
    classes: dict[str, set[str]] = {}
    counts = {"items": 0, "items_whole": 0, "items_skipped": 0, "ambiguous_dropped": 0}
    for text, cls in forbidden:
        counts["items"] += 1
        entries = scheme.entries(text)
        if not entries:
            counts["items_skipped"] += 1
            continue
        if len(normalize(text).split()) < scheme.max:
            counts["items_whole"] += 1
        for h in entries:
            if h in member:
                counts["ambiguous_dropped"] += 1
            else:
                classes.setdefault(h, set()).add(cls)
    counts["hashes"] = len(classes)
    block = {"scheme": scheme.name, "hashes": sorted(classes), "classes": {h: sorted(c) for h, c in sorted(classes.items())},
             "counts": counts}
    if key:
        block["key_hex"] = key.hex()
    return block


def json_strings(value, depth: int = 0) -> Iterable[str]:
    """Every string in a JSON value, recursing into strings that are themselves JSON documents (as oracles does)."""
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
        stripped = value.strip()
        if stripped[:1] in ("{", "[") and len(stripped) < (1 << 22):
            try:
                inner = json.loads(stripped)
            except ValueError:
                return
            yield from json_strings(inner, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from json_strings(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item, depth + 1)


class HashedShingleScanner:
    """CanaryScanner's interface (oracles.py:246-314) over a census's hashed shingles. `scan(data, exclude=())` returns
    at most one hit per (class, mode) as {"canary_id": "shingle:<class>", "mode", "shingles"}, "shingles" counting the
    distinct matching hashes. Modes: "shingle" (a `max`-word run) and "short_run" (a shorter run: under `build`'s rule, a whole short
    forbidden text).
    Classes come from the block's `classes` map when it has one, else every hit is class "forbidden". `exclude` names
    classes to skip. Text in, classes and counts out."""
    MODES = ("shingle", "short_run")

    def __init__(self, block: Mapping):
        self.scheme = Scheme.parse(block)
        hashes = block.get("hashes", ())
        if not isinstance(hashes, list) or not all(isinstance(h, str) and _HEX64.fullmatch(h) for h in hashes):
            raise ValueError("shingles: hashes must be a list of 64-hex strings")
        mapping = block.get("classes") or {}
        self.hashes = {h: tuple(mapping.get(h) or ("forbidden",)) for h in hashes}

    @classmethod
    def from_census(cls, census: Mapping) -> "HashedShingleScanner":
        return cls(census["shingles"])

    def __len__(self) -> int:
        return len(self.hashes)

    @staticmethod
    def texts(data) -> list[str]:
        """The raw text plus every string inside it when it is JSON (bodies, MCP content text, nested JSON)."""
        raw = bytes(data).decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        texts = [raw]
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            value = None
        if value is not None:
            texts.extend(json_strings(value))
        return texts

    def scan(self, data, *, exclude: Iterable[str] = ()) -> list[dict]:
        skip = set(exclude)
        found: dict[tuple[str, str], set[str]] = {}
        for text in self.texts(data):
            for h, size in self.scheme.windows(text).items():
                mode = "shingle" if size == self.scheme.max else "short_run"
                for cls in self.hashes.get(h, ()):
                    if cls not in skip:
                        found.setdefault((cls, mode), set()).add(h)
        return [{"canary_id": "shingle:" + cls, "mode": mode, "shingles": len(hs)}
                for (cls, mode), hs in sorted(found.items())]
