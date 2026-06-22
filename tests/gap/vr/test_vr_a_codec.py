"""Gap tests for vector retrieval Phase A."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.vector_codec import (
    decode_f32,
    decode_json,
    decode_vector,
    dot_similarity,
    encode_f32,
    is_normalized,
    normalize_vector,
)
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_encode_decode_f32_roundtrip() -> None:
    vector = [float(i) / 100.0 for i in range(384)]
    blob = encode_f32(vector)
    decoded = decode_f32(blob)
    assert len(decoded) == 384
    for a, b in zip(vector, decoded):
        assert abs(a - b) < 1e-6


def test_decode_json_legacy_blob() -> None:
    blob = json.dumps([0.1, 0.2, 0.3]).encode("utf-8")
    assert decode_json(blob) == [0.1, 0.2, 0.3]
    assert decode_vector(blob, "json") == [0.1, 0.2, 0.3]


def test_normalize_and_dot_similarity() -> None:
    vec = normalize_vector([3.0, 4.0])
    assert is_normalized(vec)
    assert dot_similarity(vec, vec) == pytest.approx(1.0, abs=1e-5)
