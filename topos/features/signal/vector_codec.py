"""Encode/decode embedding vectors for signal_embeddings storage."""

from __future__ import annotations

import json
import math
import struct
from typing import List, Sequence

_FORMAT_F32 = "f32"
_FORMAT_JSON = "json"
_F32_STRUCT = struct.Struct(f"<{1}f")


class VectorCodecError(ValueError):
    pass


def encode_f32(vector: Sequence[float]) -> bytes:
    if not vector:
        return b""
    return struct.pack(f"<{len(vector)}f", *[float(x) for x in vector])


def decode_f32(blob: bytes) -> List[float]:
    if not blob:
        return []
    if len(blob) % 4 != 0:
        raise VectorCodecError("f32 blob length must be a multiple of 4")
    count = len(blob) // 4
    return [float(x) for x in struct.unpack(f"<{count}f", blob)]


def decode_json(blob: bytes) -> List[float]:
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VectorCodecError("invalid json vector blob") from exc
    if not isinstance(parsed, list):
        raise VectorCodecError("json vector must be a list")
    return [float(x) for x in parsed]


def decode_vector(blob: bytes, vector_format: str) -> List[float]:
    fmt = (vector_format or _FORMAT_JSON).strip().lower()
    if fmt == _FORMAT_F32:
        return decode_f32(blob)
    if fmt == _FORMAT_JSON:
        return decode_json(blob)
    raise VectorCodecError(f"unknown vector_format: {vector_format}")


def encode_vector(vector: Sequence[float], vector_format: str) -> bytes:
    fmt = (vector_format or _FORMAT_F32).strip().lower()
    if fmt == _FORMAT_F32:
        return encode_f32(vector)
    if fmt == _FORMAT_JSON:
        return json.dumps([float(x) for x in vector]).encode("utf-8")
    raise VectorCodecError(f"unknown vector_format: {vector_format}")


def l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in vector))


def is_normalized(vector: Sequence[float], tol: float = 1e-5) -> bool:
    norm = l2_norm(vector)
    if norm == 0.0:
        return False
    return abs(norm - 1.0) <= tol


def normalize_vector(vector: Sequence[float]) -> List[float]:
    norm = l2_norm(vector)
    if norm <= 0:
        return [float(x) for x in vector]
    return [float(x) / norm for x in vector]


def dot_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    return sum(float(x) * float(y) for x, y in zip(a, b))


def similarity(
    query_vector: Sequence[float],
    stored_vector: Sequence[float],
    *,
    normalized: bool = True,
) -> float:
    if normalized and is_normalized(query_vector) and is_normalized(stored_vector):
        return dot_similarity(query_vector, stored_vector)
    from .vector_math import cosine_similarity

    return cosine_similarity(list(query_vector), list(stored_vector))
