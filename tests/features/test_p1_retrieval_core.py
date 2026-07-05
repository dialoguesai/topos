"""P1 unit tests: embedding prefixes, context headers, RRF fusion, dims-dynamic ANN."""

from __future__ import annotations

import sqlite3

import pytest

from topos.engine.backends.huggingface import (
    apply_embedding_prefix,
    active_embedding_model,
    embedding_model_profile,
)
from topos.features.signal.embed_context import (
    build_embed_text,
    context_header,
    embeddable_content,
)
from topos.query.retrieval import _rrf_fuse_summary_lists


class TestEmbeddingPrefixes:
    def test_minilm_is_symmetric(self) -> None:
        texts = ["hello"]
        assert (
            apply_embedding_prefix(
                texts, model_name="sentence-transformers/all-MiniLM-L6-v2", input_role="query"
            )
            == texts
        )

    def test_bge_query_prefix_applied(self) -> None:
        out = apply_embedding_prefix(
            ["knee pain"], model_name="BAAI/bge-small-en-v1.5", input_role="query"
        )
        assert out[0].startswith("Represent this sentence for searching relevant passages: ")
        # bge passages are unprefixed
        out_p = apply_embedding_prefix(
            ["knee pain"], model_name="BAAI/bge-small-en-v1.5", input_role="passage"
        )
        assert out_p == ["knee pain"]

    def test_e5_both_roles_prefixed(self) -> None:
        q = apply_embedding_prefix(["x"], model_name="intfloat/e5-small-v2", input_role="query")
        p = apply_embedding_prefix(["x"], model_name="intfloat/e5-small-v2", input_role="passage")
        assert q == ["query: x"] and p == ["passage: x"]

    def test_env_switches_active_model(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        assert active_embedding_model() == "BAAI/bge-small-en-v1.5"
        assert embedding_model_profile()["dims"] == 384

    def test_unknown_model_defaults_to_no_prefix(self) -> None:
        assert apply_embedding_prefix(["x"], model_name="acme/mystery", input_role="query") == ["x"]


class TestContextHeaders:
    def test_journal_header_includes_kind_date_place(self) -> None:
        msg = {
            "canonical_table": "journal_entries",
            "entry_at": "2026-05-06T21:15:00Z",
            "place_name": "Mudlark Studio",
            "content": "Threw four bowls.",
        }
        header = context_header(msg)
        assert "journal" in header and "2026-05-06" in header and "Mudlark Studio" in header

    def test_headers_disabled_by_env(self, monkeypatch) -> None:
        monkeypatch.setenv("TOPOS_EMBED_CONTEXT_HEADERS", "off")
        assert context_header({"canonical_table": "journal_entries"}) == ""
        assert build_embed_text({"canonical_table": "journal_entries"}, "chunk") == "chunk"

    def test_profile_fallback_content_includes_org(self) -> None:
        row = {
            "record_type": "experience",
            "title": "Data Engineer",
            "organization": "Lumon Industries",
            "description": "Built the ETL platform.",
        }
        content = embeddable_content(row)
        assert "Lumon Industries" in content and "Data Engineer" in content

    def test_embed_text_prepends_header_keeps_chunk(self) -> None:
        msg = {"canonical_table": "calendar_events", "starts_at": "2026-06-28T08:00:00Z", "title": "Race day"}
        text = build_embed_text(msg, "Half marathon start")
        assert text.endswith("Half marathon start")
        assert "calendar" in text.splitlines()[0]


class TestRrfFusion:
    def test_multi_list_item_outranks_single_list_top(self) -> None:
        shared = {"record_id": "r1", "topic": "shared", "retrieval_source": "vector"}
        fused = _rrf_fuse_summary_lists(
            [
                ("vector", 1.0, [shared, {"record_id": "r2", "topic": "v2", "retrieval_source": "vector"}]),
                ("canonical", 1.0, [{"record_id": "r1", "topic": "shared", "retrieval_source": "canonical:x"}]),
                ("briefs", 0.8, [{"topic": "brief", "retrieval_source": "dimension_brief"}]),
            ]
        )
        assert fused[0]["record_id"] == "r1"
        assert fused[0]["relevance_score"] == 1.0
        assert set(fused[0]["fusion_sources"]) == {"vector", "canonical"}

    def test_scores_normalized_to_unit_interval(self) -> None:
        fused = _rrf_fuse_summary_lists(
            [("vector", 1.0, [{"record_id": f"r{i}", "topic": str(i)} for i in range(5)])]
        )
        assert all(0.0 < item["relevance_score"] <= 1.0 for item in fused)
        assert fused == sorted(fused, key=lambda i: i["relevance_score"], reverse=True)

    def test_weight_biases_ranking(self) -> None:
        fused = _rrf_fuse_summary_lists(
            [
                ("canonical", 2.0, [{"record_id": "canon", "topic": "c"}]),
                ("vector", 1.0, [{"record_id": "vec", "topic": "v"}]),
            ]
        )
        assert fused[0]["record_id"] == "canon"

    def test_empty_lists_return_empty(self) -> None:
        assert _rrf_fuse_summary_lists([("vector", 1.0, [])]) == []


class TestDimsDynamicVecTable:
    def test_declared_dims_parsed_and_sync_respects_them(self) -> None:
        from topos.storage.db.migrations.vector_storage_v4 import declared_vec_dims

        conn = sqlite3.connect(":memory:")
        # Plain table stand-in: declared_vec_dims only parses DDL text.
        conn.execute(
            "CREATE TABLE signal_embeddings_vec (embedding_id TEXT PRIMARY KEY, embedding BLOB)"
        )
        assert declared_vec_dims(conn) == 0  # no float[N] in DDL -> unknown

        conn2 = sqlite3.connect(":memory:")
        conn2.execute(
            'CREATE TABLE "signal_embeddings_vec_ddl_probe" (x TEXT)'
        )
        # Simulate a vec0 DDL row via a view name trick is overkill; assert the
        # regex path directly instead.
        from topos.storage.db.migrations import vector_storage_v4 as v4

        class FakeConn:
            def execute(self, sql, params=()):
                class R:
                    @staticmethod
                    def fetchone():
                        return ("CREATE VIRTUAL TABLE signal_embeddings_vec USING vec0(embedding_id TEXT PRIMARY KEY, embedding float[768])",)

                return R()

        assert v4.declared_vec_dims(FakeConn()) == 768  # type: ignore[arg-type]


class TestRerankSubtypeContract:
    def test_rerank_requires_query_and_candidates(self) -> None:
        from topos.engine.backends.huggingface import HuggingFaceAdapter

        out = HuggingFaceAdapter().run_inference({"query": "", "candidates": []}, {"subtype": "rerank"})
        assert out.get("error")

    def test_service_rerank_falls_back_on_unavailable(self, monkeypatch) -> None:
        from topos.features.signal.service import SignalService

        service = SignalService.__new__(SignalService)  # no adapters needed
        items = [
            {"record_id": "a", "text_preview": "alpha", "similarity": 0.9},
            {"record_id": "b", "text_preview": "beta", "similarity": 0.8},
        ]

        import topos.engine.backends.huggingface as hf

        class FakeAdapter:
            def run_inference(self, payload, config=None):
                return {"status": "unavailable", "items": []}

        monkeypatch.setattr(hf, "HuggingFaceAdapter", FakeAdapter)
        out = SignalService._maybe_rerank(service, "query", items)
        assert out == items

    def test_service_rerank_reorders(self, monkeypatch) -> None:
        from topos.features.signal.service import SignalService

        service = SignalService.__new__(SignalService)
        items = [
            {"record_id": "a", "text_preview": "alpha", "similarity": 0.9},
            {"record_id": "b", "text_preview": "beta", "similarity": 0.8},
        ]

        import topos.engine.backends.huggingface as hf

        class FakeAdapter:
            def run_inference(self, payload, config=None):
                return {
                    "items": [
                        {"id": 1, "rerank_score": 5.0},
                        {"id": 0, "rerank_score": 1.0},
                    ]
                }

        monkeypatch.setattr(hf, "HuggingFaceAdapter", FakeAdapter)
        out = SignalService._maybe_rerank(service, "query", items)
        assert [i["record_id"] for i in out] == ["b", "a"]
        assert out[0]["rerank_score"] == 5.0
