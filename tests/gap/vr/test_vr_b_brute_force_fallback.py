"""Gap tests for ANN fallback (Phase B)."""

from __future__ import annotations

import os

import pytest

from topos.storage.adapters.fakes import InMemoryVectorIndex

pytestmark = pytest.mark.gap


def test_brute_force_search_with_ann_disabled(monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_VECTOR_ANN", "brute_force")
    index = InMemoryVectorIndex()
    index.upsert(
        {
            "embedding_id": "e1",
            "record_id": "r1",
            "model": "m1",
            "source_id": "s1",
            "chunk_index": 0,
        },
        vector=[1.0, 0.0, 0.0],
    )
    page = index.search_similar([0.9, 0.1, 0.0], limit=1)
    assert page.items[0]["embedding_id"] == "e1"
