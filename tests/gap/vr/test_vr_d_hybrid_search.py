"""Gap tests for hybrid search helpers (Phase D)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.hybrid_search import reciprocal_rank_fusion
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_reciprocal_rank_fusion_prefers_overlap() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])
    assert fused["b"] > fused["c"]
    assert fused["b"] > fused["d"]


def test_vector_search_filters_below_min_similarity(monkeypatch) -> None:
    from topos.features.signal.service import SignalService
    from topos.storage.adapters.factory import AdapterFactory

    adapters = AdapterFactory.create("memory")

    class _FakePage:
        total = 2
        items = [
            {"record_id": "r1", "similarity": 0.05, "text_preview": "noise"},
            {"record_id": "r2", "similarity": 0.85, "text_preview": "relevant"},
        ]

    monkeypatch.setattr(adapters.vector, "search_similar", lambda *args, **kwargs: _FakePage())
    monkeypatch.setattr(
        "topos.features.signal.vector_settings.min_similarity_threshold",
        lambda: 0.30,
    )
    monkeypatch.setattr(
        "topos.features.signal.vector_settings.vector_hybrid_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "topos.features.signal.query_embed_cache.get_cached_query_embedding",
        lambda *args, **kwargs: [0.1] * 8,
    )
    svc = SignalService(adapters)
    result = svc.search_vectors(query="xyzzy nonsense query", limit=10, hydrate=False)
    assert len(result["items"]) == 1
    assert result["items"][0]["record_id"] == "r2"


def test_fts_table_created_after_migration(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "fts.db"))
    ensure_migrations_applied(conn)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='signal_embeddings_fts'"
    ).fetchone()
    assert row is not None
