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


def _search_service(monkeypatch, page_items):
    """SignalService wired to a fixed candidate page, hybrid and rerank off."""
    from topos.features.signal.service import SignalService

    adapters = AdapterFactory.create("memory")

    class _FakePage:
        total = len(page_items)
        items = page_items

    monkeypatch.setattr(adapters.vector, "search_similar", lambda *a, **k: _FakePage())
    monkeypatch.setattr(
        "topos.features.signal.vector_settings.vector_hybrid_enabled", lambda: False
    )
    monkeypatch.setattr("topos.features.signal.vector_settings.rerank_mode", lambda: "off")
    monkeypatch.setattr(
        "topos.features.signal.query_embed_cache.get_cached_query_embedding",
        lambda *a, **k: [0.1] * 8,
    )
    return SignalService(adapters)


def test_identical_text_collapses_to_one_result(monkeypatch) -> None:
    """The same text is embedded once per sighting, so a page title visited six
    times is six rows sharing one content_hash — and it took four of five result
    slots on the first live node."""
    svc = _search_service(
        monkeypatch,
        [
            {"record_id": f"browser:visit-{i}", "embedding_id": f"e{i}",
             "similarity": 0.9 - i / 100, "text_preview": "GitHub", "content_hash": "aaa"}
            for i in range(4)
        ]
        + [{"record_id": "msg-1", "embedding_id": "e9", "similarity": 0.5,
            "text_preview": "git push to main", "content_hash": "bbb"}],
    )
    items = svc.search_vectors(query="git github", limit=5, hydrate=False)["items"]

    assert [i["text_preview"] for i in items] == ["GitHub", "git push to main"]
    assert items[0]["similarity"] == 0.9, "kept a worse-scoring twin"
    assert items[0]["occurrences"] == 4, "collapsed copies vanished without a trace"
    assert items[1]["occurrences"] == 1


def test_distinct_text_from_one_record_is_not_collapsed(monkeypatch) -> None:
    """Chunks of a document share a record but say different things."""
    svc = _search_service(
        monkeypatch,
        [
            {"record_id": "doc-1", "embedding_id": "c0", "similarity": 0.9,
             "text_preview": "chapter one", "content_hash": "h1"},
            {"record_id": "doc-2", "embedding_id": "c1", "similarity": 0.8,
             "text_preview": "chapter two", "content_hash": "h2"},
        ],
    )
    items = svc.search_vectors(query="chapters", limit=5, hydrate=False)["items"]
    assert len(items) == 2


def test_page_still_fills_when_duplicates_collapse(monkeypatch) -> None:
    """Collapsing happens after the fetch, so the fetch has to be wider than the
    page or a duplicate-heavy corpus returns short pages."""
    page = [
        {"record_id": f"dup-{i}", "embedding_id": f"d{i}", "similarity": 0.9,
         "text_preview": "same thing", "content_hash": "same"}
        for i in range(6)
    ] + [
        {"record_id": f"uniq-{i}", "embedding_id": f"u{i}", "similarity": 0.8 - i / 100,
         "text_preview": f"distinct {i}", "content_hash": f"u{i}"}
        for i in range(3)
    ]
    svc = _search_service(monkeypatch, page)
    items = svc.search_vectors(query="anything", limit=3, hydrate=False)["items"]
    assert len(items) == 3, "page came back short after collapsing duplicates"
    assert items[0]["occurrences"] == 6


def test_fts_table_created_after_migration(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "fts.db"))
    ensure_migrations_applied(conn)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='signal_embeddings_fts'"
    ).fetchone()
    assert row is not None
