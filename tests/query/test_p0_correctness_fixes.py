"""Regression tests for the P0 correctness fixes (dense-intelligence upgrade).

Covers:
- FTS-only hybrid hits survive the default cosine similarity threshold
- bare day numbers no longer fabricate March dates
- ANN filter starvation falls back to brute force
- small-batch recluster gating
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.hybrid_search import merge_hybrid_results
from topos.query.retrieval import _iso_date_hints


@pytest.fixture()
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            signal_dimension TEXT,
            model TEXT,
            event_at TEXT,
            text_preview TEXT,
            search_text TEXT,
            vector_blob BLOB,
            vector_format TEXT,
            provenance_json TEXT
        )
        """
    )
    yield conn
    conn.close()


class TestHybridSimilarityScale:
    def test_fts_only_hit_keeps_no_cosine_similarity(self, conn) -> None:
        """An FTS-only hit must not masquerade its RRF score as a cosine."""
        conn.execute(
            "INSERT INTO signal_embeddings (embedding_id, record_id, provenance_json)"
            " VALUES ('emb_fts', 'rec_fts', '{\"record_id\": \"rec_fts\", \"text_preview\": \"quarterly taxes\"}')"
        )
        vector_items = [
            {"embedding_id": "emb_vec", "record_id": "rec_vec", "similarity": 0.71}
        ]
        merged = merge_hybrid_results(conn, vector_items, ["emb_fts"], limit=10)
        fts_item = next(i for i in merged if i.get("record_id") == "rec_fts")
        vec_item = next(i for i in merged if i.get("record_id") == "rec_vec")

        assert fts_item.get("similarity") is None, (
            "FTS-only hit was assigned a fake similarity — any cosine threshold "
            ">= ~0.03 would silently drop every keyword-only match"
        )
        assert fts_item["hybrid_score"] > 0
        assert vec_item["similarity"] == 0.71

    def test_threshold_filter_semantics(self, conn) -> None:
        """Simulates service.search_vectors: threshold must only bind cosine values."""
        conn.execute(
            "INSERT INTO signal_embeddings (embedding_id, record_id, provenance_json)"
            " VALUES ('emb_fts', 'rec_fts', '{\"record_id\": \"rec_fts\"}')"
        )
        merged = merge_hybrid_results(
            conn,
            [{"embedding_id": "emb_vec", "record_id": "rec_vec", "similarity": 0.55}],
            ["emb_fts"],
            limit=10,
        )
        min_sim = 0.30  # the production default
        surviving = [
            item
            for item in merged
            if item.get("similarity") is None or float(item["similarity"]) >= min_sim
        ]
        assert {i.get("record_id") for i in surviving} == {"rec_vec", "rec_fts"}


class TestDateHints:
    def test_month_name_day_still_parses(self) -> None:
        assert _iso_date_hints("Compare March 13 vs March 16 2026") == [
            "2026-03-13",
            "2026-03-16",
        ]

    def test_bare_day_number_returns_nothing(self) -> None:
        """'meet on the 5th' used to fabricate <year>-03-05."""
        assert _iso_date_hints("can we meet on the 5th?") == []

    def test_bare_number_in_ordinary_text_returns_nothing(self) -> None:
        assert _iso_date_hints("I bought 3 tickets") == []

    def test_abbreviated_month_parses(self) -> None:
        import datetime

        year = datetime.datetime.now(datetime.timezone.utc).year
        assert _iso_date_hints("dinner on Sep 9") == [f"{year}-09-09"]

    def test_iso_date_passthrough(self) -> None:
        assert _iso_date_hints("what happened on 2025-11-02") == ["2025-11-02"]


class TestAnnStarvationFallback:
    def test_underfilled_filtered_ann_returns_none(self, monkeypatch) -> None:
        """Filtered ANN results below the limit must trigger brute-force fallback."""
        from topos.storage.adapters.sqlite import vector_search as vs

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE signal_embeddings_vec (embedding_id TEXT, embedding BLOB)")
        conn.execute(
            """
            CREATE TABLE signal_embeddings (
                embedding_id TEXT PRIMARY KEY, source_id TEXT, signal_dimension TEXT,
                model TEXT, event_at TEXT, provenance_json TEXT
            )
            """
        )
        # Three ANN candidates; only one matches the source filter.
        for idx in range(3):
            sid = "wanted" if idx == 0 else "other"
            conn.execute(
                "INSERT INTO signal_embeddings VALUES (?, ?, 'memory', 'm', NULL, ?)",
                (f"e{idx}", sid, f'{{"record_id": "r{idx}"}}'),
            )

        def fake_execute_knn(*args, **kwargs):
            return [(f"e{i}", 0.1 * (i + 1)) for i in range(3)]

        monkeypatch.setattr(vs, "_sqlite_vec_ready", lambda c: True)

        class FakeConn:
            def execute(self, sql, params=()):
                if "MATCH" in sql:
                    class R:
                        @staticmethod
                        def fetchall():
                            return fake_execute_knn()

                    return R()
                return conn.execute(sql, params)

        result = vs.search_similar_ann(
            FakeConn(),  # type: ignore[arg-type]
            [0.0] * 384,
            source_id="wanted",
            limit=2,
        )
        assert result is None, "starved filtered ANN result must fall back to exact search"


class TestReclusterGate:
    def test_recent_recluster_detected(self) -> None:
        from topos.enrichment.jobs.canonical.topic_clusters_job import (
            _recent_recluster_exists,
        )

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE topic_clusters (cluster_id TEXT, updated_at TEXT)")
        assert _recent_recluster_exists(conn) is False
        conn.execute(
            "INSERT INTO topic_clusters VALUES ('tc_1', datetime('now'))"
        )
        assert _recent_recluster_exists(conn) is True
        conn.execute("DELETE FROM topic_clusters")
        conn.execute(
            "INSERT INTO topic_clusters VALUES ('tc_2', datetime('now', '-2 hours'))"
        )
        assert _recent_recluster_exists(conn) is False
