"""Topos canonical JSON v1, deliberately narrower than general JSON/JCS.

ASCII keys, verbatim Unicode values, Python-compatible lowercase UTF-16 escapes,
safe integers, no floats. Array order is significant; no semantic normalization.
"""
from __future__ import annotations

import hashlib
import json
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
