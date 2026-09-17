"""Topos canonical JSON v1, deliberately narrower than general JSON/JCS.

ASCII keys, verbatim Unicode values, Python-compatible lowercase UTF-16 escapes,
safe integers, no floats. Array order is significant; no semantic normalization.
"""
from __future__ import annotations

import hashlib
import json
from json.encoder import encode_basestring_ascii as _ascii_string  # the C escaper json.dumps(ensure_ascii=True) uses
import re
from typing import Any

MAX_INTEGER = 2**53 - 1
MAX_BYTES = 1_048_576
MAX_DEPTH = 40


class PolicyError(ValueError):
    """A bounded machine reason, with no candidate data in the exception."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _check(value: Any, depth: int = 0) -> None:
    # `_cell` below re-implements this function's scalar branch at C speed for the
    # streamed encoder. Any change to the rules here must change it too; the
    # equality is pinned by tests/permissions_v2/test_canonical_stream.py.
    if depth > MAX_DEPTH:
        raise PolicyError("json_depth")
    if value is None or type(value) is bool:
        return
    if type(value) is int and abs(value) <= MAX_INTEGER:
        return
    if type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise PolicyError("json_surrogate")
        return
    if type(value) is list:
        for item in value:
            _check(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise PolicyError("json_key")
            _check(item, depth + 1)
        return
    raise PolicyError("json_type")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(encoded) > MAX_BYTES:
        raise PolicyError("json_size")
    return encoded


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("json_duplicate_key")
        result[key] = value
    return result


def _invalid_number(_: str) -> None:
    raise PolicyError("json_number")


def parse_json(raw: bytes | str) -> Any:
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_BYTES:
        raise PolicyError("json_size")
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        value = json.loads(raw, object_pairs_hook=_pairs, parse_float=_invalid_number, parse_constant=_invalid_number)
        canonical_bytes(value)
        return value
    except PolicyError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise PolicyError("json_invalid") from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# --- the same canonical bytes, streamed, for whole-table digests --------------------------------
#
# `canonical_bytes` refuses any value whose encoding exceeds MAX_BYTES, which caps a digest
# taken over a whole table at roughly 1 MiB of rows. `digest_stream` feeds the identical byte
# string to SHA-256 in bounded chunks instead of building it, so a table digest is bounded by
# the time to read the rows rather than by the size of one Python object. The value is not a
# new scheme: for every table `digest` accepts, `digest_stream` returns the same hex, and for
# every table `digest` refuses it raises the same code -- except `json_size`, which it never
# raises. Callers that need a bounded, quotable encoding keep using `canonical_bytes`.
#
# Removing a size bound is only safe on locally derived rows, so `digest_stream` refuses a
# value that streams nothing: the value must be `Rows`/`MappingRows`, or a dict holding at
# least one of them. That is what stops the helper being reused as a general cap remover on
# request-derived data, where the 1 MiB refusal is the bound on the work one request can ask
# for. The row supply is the only unbounded part; every other value in the dict still goes
# through the materializing encoder.
#
# Domain separation is the caller's, exactly as it is for `digest`: nothing here is signed or
# reused across schemes. The review stores separate their two families by the version literal
# and `store_id` in the enrollment marker each digest is compared against, never by the bytes.

_SURROGATE = re.compile("[\ud800-\udfff]")
_CHUNK = 1 << 16


class Rows:
    """A JSON array of sequence rows, supplied lazily: item `i` encodes as `list(item)` would.

    `Rows(cursor)` therefore encodes as `[list(row) for row in cursor.fetchall()]`
    without ever holding the table in memory, which is the shape every
    `digest({table: [list(row) for row in ...]})` ledger already digests.

    A row that is not a list or a tuple is refused with `json_type` rather than
    encoded. `list(mapping)` is the mapping's KEYS, so a mapping row would
    otherwise digest its column names and drop every value -- a different,
    perfectly well-formed answer, which is the one outcome a digest helper must
    never return. A ledger whose rows are mappings uses `MappingRows`.
    """
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


class MappingRows(Rows):
    """A JSON array of mapping rows: item `i` encodes exactly as the dict itself would.

    `MappingRows(dict(zip(columns, row)) for row in cursor)` encodes as
    `[{"column":value,...}, ...]` with each row's ASCII keys sorted, which is
    what `digest({"rows": [dict, ...]})` produces today -- so a dict-row ledger
    (`exclusion_floor.exclusion_fingerprint`) can adopt the streamed encoder
    without moving its pinned fingerprint.
    """
    __slots__ = ()


_ROW_KINDS = (Rows, MappingRows)


class _Sink:
    __slots__ = ("hash", "parts", "size")

    def __init__(self):
        self.hash, self.parts, self.size = hashlib.sha256(), [], 0

    def put(self, piece: str) -> None:
        self.parts.append(piece)
        self.size += len(piece)
        if self.size >= _CHUNK:
            self.flush()

    def flush(self) -> None:
        if self.parts:
            self.hash.update("".join(self.parts).encode("ascii"))
            self.parts, self.size = [], 0


class _Collector:
    __slots__ = ("parts",)

    def __init__(self):
        self.parts = []

    def put(self, piece: str) -> None:
        self.parts.append(piece)


def _cell(value: Any, depth: int):
    """The scalar branch of `_check`, encoded in one pass; None means "a container, use `_encode`"."""
    if depth > MAX_DEPTH:
        raise PolicyError("json_depth")
    kind = type(value)
    if kind is str:
        # `_check` refuses any code point in U+D800..U+DFFF; ASCII text cannot hold one.
        if not value.isascii() and _SURROGATE.search(value) is not None:
            raise PolicyError("json_surrogate")
        return _ascii_string(value)
    if kind is int:
        if abs(value) > MAX_INTEGER:
            raise PolicyError("json_type")
        return int.__repr__(value)
    if value is None:
        return "null"
    if kind is bool:
        return "true" if value else "false"
    if kind is list or kind is dict:
        return None
    raise PolicyError("json_type")


def _encode(value: Any, depth: int, sink) -> None:
    """The materialized part: `_check` and `json.dumps` verbatim, so bytes and codes both match."""
    _check(value, depth)
    sink.put(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _encode_rows(rows: Rows, depth: int, sink) -> None:
    # A JSON array encodes as "[" + ",".join(item encodings) + "]", and each item encodes
    # independently, so the concatenation below is the same byte string json.dumps produces.
    # The depth arithmetic is pinned by direct tests in tests/permissions_v2/test_canonical_stream.py:
    # today's two call sites enter at depth 0 and 1, so no store can reach MAX_DEPTH, and the
    # guards exist so that a third call site cannot silently lose the rule.
    if depth > MAX_DEPTH:
        raise PolicyError("json_depth")
    mapping = type(rows) is MappingRows
    sink.put("[")
    first = True
    for row in rows.items:
        if depth + 1 > MAX_DEPTH:
            raise PolicyError("json_depth")
        if mapping:
            # One mapping row at a time, through the materializing encoder: the rows stream,
            # the row does not, and the bytes are `json.dumps(row, sort_keys=True)` exactly.
            if type(row) is not dict:
                raise PolicyError("json_type")
            collector = _Collector()
            _encode(row, depth + 1, collector)
            sink.put(("" if first else ",") + "".join(collector.parts))
            first = False
            continue
        if type(row) is not list and type(row) is not tuple:
            # `list(item)` of a mapping is its keys, so encoding it would answer a different
            # question instead of refusing. Mapping rows go through `MappingRows`.
            raise PolicyError("json_type")
        pieces = []
        for cell in row:
            piece = _cell(cell, depth + 2)
            if piece is None:  # a container cell; sqlite3 never produces one
                collector = _Collector()
                _encode(cell, depth + 2, collector)
                piece = "".join(collector.parts)
            pieces.append(piece)
        sink.put(("[" if first else ",[") + ",".join(pieces) + "]")
        first = False
    sink.put("]")


def digest_stream(value: Any) -> str:
    """`digest(value)`, computed without materializing the rows and without the size cap.

    `Rows` (or `MappingRows`) may be the whole value or a value of a top-level
    dict, which is the shape a multi-table ledger digest needs. When such a dict
    holds more than one fault, the reported code may be a different one of them
    than `_check` would report; both refuse.

    A value that streams nothing is refused with `json_type`. The cap this
    function does not apply is the bound on how much work one value can ask for,
    so it is lifted only for rows a caller reads out of its own store, never for
    a value a caller happened to build: `canonical_bytes` and `digest` keep it.
    """
    sink = _Sink()
    if type(value) in _ROW_KINDS:
        _encode_rows(value, 0, sink)
    elif type(value) is dict and any(type(item) in _ROW_KINDS for item in value.values()):
        for key in value:
            if type(key) is not str or not key.isascii():
                raise PolicyError("json_key")
        sink.put("{")
        for index, key in enumerate(sorted(value)):
            sink.put(("," if index else "") + _ascii_string(key) + ":")
            item = value[key]
            if type(item) in _ROW_KINDS:
                _encode_rows(item, 1, sink)
            else:
                _encode(item, 1, sink)
        sink.put("}")
    else:
        raise PolicyError("json_type")
    sink.flush()
    return sink.hash.hexdigest()
