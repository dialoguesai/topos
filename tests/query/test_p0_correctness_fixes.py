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


class TestSameMonthDayRanges:
    """A day range inside one month must span, not collapse to its first day.

    Live failure 2026-08-17: "generate a personal work report for Aug 11-16, 2026"
    searched Aug 11 alone, found little, and reported the rest of the week as unsynced
    data. The window it returned was well-formed, so nothing looked broken. The
    repeated-month and cross-month spellings already worked, which is what let the
    compact form - the one people actually type - stay broken.
    """

    @pytest.mark.parametrize("text", [
        "Aug 11\u201316, 2026",      # en dash, as pasted from most editors
        "Aug 11\u201416, 2026",      # em dash
        "Aug 11-16, 2026",
        "August 11-16, 2026",
        "Aug 11 to 16, 2026",
        "Aug 11 through 16 2026",
        "August 11th-16th, 2026",
    ])
    def test_compact_range_yields_both_endpoints(self, text: str) -> None:
        assert _iso_date_hints(text) == ["2026-08-11", "2026-08-16"]

    @pytest.mark.parametrize("text", [
        "Aug 11 through Aug 16 2026",
        "August 11 to August 16, 2026",
    ])
    def test_repeated_month_forms_are_unchanged(self, text: str) -> None:
        assert _iso_date_hints(text) == ["2026-08-11", "2026-08-16"]

    def test_cross_month_range_is_unchanged(self) -> None:
        assert _iso_date_hints("Aug 28 - Sep 3, 2026") == ["2026-08-28", "2026-09-03"]

    def test_the_planner_spans_the_whole_range(self) -> None:
        from topos.query.planner import _explicit_time_range

        assert _explicit_time_range("a work report for Aug 11\u201316, 2026") == (
            "2026-08-11T00:00:00+00:00",
            "2026-08-16T23:59:59+00:00",
        )

    def test_a_bare_number_range_is_not_a_date(self) -> None:
        """The range pattern must need a month; "11-16" alone is a quantity."""
        assert _iso_date_hints("what did I spend 11-16 dollars on") == []

    def test_may_ranges_behave_like_may_single_dates(self) -> None:
        """Ranges inherit the existing "may" treatment rather than inventing a new one.

        `"I may 11 times"` already yields 2026-05-11 on the single-date path — the full
        month-name pattern accepts "may" even though the *abbreviation* list deliberately
        omits it. That over-match predates day ranges and is a real (separate) bug; what
        matters here is that the range form is not MORE permissive than the single form,
        so a fix applies to one place and covers both.
        """
        assert _iso_date_hints("I may 11-16 times reconsider") == [
            "2026-05-11",
            "2026-05-16",
        ]
        # Consistency with the single-date path, which has behaved this way all along.
        assert _iso_date_hints("I may 11 times reconsider") == ["2026-05-11"]
        # The guard that does hold: no digits, no date.
        assert _iso_date_hints("I may go running") == []

    def test_impossible_days_are_dropped_not_fabricated(self) -> None:
        assert _iso_date_hints("Feb 29-30, 2026") == []
        assert _iso_date_hints("Aug 15-99, 2026") == ["2026-08-15"]


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
