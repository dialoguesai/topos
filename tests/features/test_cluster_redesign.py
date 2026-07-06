"""Cluster redesign regression suite (2026-07-06 audit fixes).

Pins the four structural fixes:
  1. k is no longer capped at 6 per facet (the cap that collapsed 20k records
     into six mega-clusters);
  2. serialization junk is excluded from cluster input;
  3. incremental assignment is strict (0.75) and never nudges centroids
     (the rich-get-richer loop);
  4. every full recompute logs quality metrics, and LLM labels degrade to
     term labels on any model failure.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3

import pytest

from topos.features.signal import incremental_clustering as inc
from topos.features.signal import topic_clustering as tc
from topos.features.signal.cluster_labels import (
    apply_llm_cluster_labels,
    build_label_prompt,
    parse_label,
)
from topos.features.signal.vector_codec import encode_f32


def _blob_vector(dim_hot: int, dims: int = 8) -> list[float]:
    vec = [0.0] * dims
    vec[dim_hot % dims] = 1.0
    return vec


def _records_in_blobs(n_blobs: int, per_blob: int, dims: int = 8) -> list[dict]:
    rng = random.Random(7)
    records = []
    for blob in range(n_blobs):
        for i in range(per_blob):
            base = _blob_vector(blob, dims)
            noisy = [x + rng.uniform(-0.05, 0.05) for x in base]
            norm = math.sqrt(sum(v * v for v in noisy))
            records.append(
                {
                    "record_id": f"r_{blob}_{i}",
                    "source_id": "chatgpt_file_ingestion",
                    "signal_dimension": "memory",
                    "text_preview": f"blob {blob} item {i}",
                    "vector": [v / norm for v in noisy],
                    "record_type": "ai_chat_message",
                    "metadata": {},
                }
            )
    return records


class TestKUncapped:
    def test_choose_k_scales_with_sqrt_n(self):
        from topos.features.signal.vector_settings import cluster_max_k

        assert tc._choose_k(20000, max_clusters=cluster_max_k()) == 64
        assert tc._choose_k(900, max_clusters=cluster_max_k()) == 30

    def test_max_k_env_override(self, monkeypatch):
        from topos.features.signal.vector_settings import cluster_max_k

        monkeypatch.setenv("TOPOS_CLUSTER_MAX_K", "128")
        assert cluster_max_k() == 128

    def test_facet_clustering_breaks_the_old_cap_of_six(self):
        # 10 well-separated blobs in one facet must yield ~10 clusters, not 6.
        records = _records_in_blobs(n_blobs=10, per_blob=30)
        clusters = tc.cluster_records_by_facet(records)
        assert len(clusters) >= 8, (
            f"only {len(clusters)} clusters for 10 separated blobs — k cap regressed"
        )
        sizes = sorted((c["member_count"] for c in clusters), reverse=True)
        assert sizes[0] / sum(sizes) < 0.25


class TestJunkExclusion:
    @pytest.fixture()
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE signal_embeddings (
                embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT,
                signal_dimension TEXT, text_preview TEXT, vector_blob BLOB,
                provenance_json TEXT, vector_format TEXT, chunk_index INTEGER DEFAULT 0
            )
            """
        )
        rows = [
            ("e1", "r1", "chatgpt_file_ingestion", "memory", "real conversation about training",
             encode_f32(_blob_vector(0)), "{}", "f32", 0),
            ("e2", "r2", "chatgpt_file_ingestion", "memory",
             "streamtyped NSMutableAttributedString NSKeyedArchiver blob",
             encode_f32(_blob_vector(1)), "{}", "f32", 0),
            ("e3", "r3", "chatgpt_file_ingestion", "memory", "notes on the topos launch",
             encode_f32(_blob_vector(2)), "{}", "f32", 0),
        ]
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        yield conn
        conn.close()

    def test_junk_previews_never_enter_clustering(self, conn):
        records = tc.load_embedding_records(conn)
        ids = {r["record_id"] for r in records}
        assert ids == {"r1", "r3"}


class TestStrictAssignment:
    @pytest.fixture()
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE topic_clusters (
                cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
                member_count INTEGER, centroid_vector BLOB, metadata_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE topic_cluster_members (
                member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT,
                source_id TEXT, record_type TEXT, text_preview TEXT,
                weight REAL, metadata_json TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE cluster_candidates (
                embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE signal_embeddings (
                embedding_id TEXT PRIMARY KEY, cluster_id TEXT
            );
            """
        )
        centroid = _blob_vector(0)
        conn.execute(
            "INSERT INTO topic_clusters VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            ("tc_a", "blob zero", "memory", 10, encode_f32(centroid), "{}"),
        )
        yield conn
        conn.close()

    def _row(self, embedding_id: str, vector: list[float]) -> dict:
        return {
            "embedding_id": embedding_id,
            "record_id": f"rec_{embedding_id}",
            "source_id": "chatgpt_file_ingestion",
            "text_preview": "x",
            "vector": vector,
            "record_type": "ai_chat_message",
        }

    def test_below_threshold_goes_to_pool(self, conn):
        # cosine ~0.71 to the centroid: assigned under the old 0.60, pooled now.
        v = [0.0] * 8
        v[0] = 1.0
        v[1] = 1.0
        norm = math.sqrt(2)
        result = inc.assign_embeddings(conn, [self._row("e_mid", [x / norm for x in v])])
        assert result == {"assigned": 0, "pooled": 1}

    def test_clear_match_is_assigned_without_centroid_nudge(self, conn):
        before = conn.execute(
            "SELECT centroid_vector FROM topic_clusters WHERE cluster_id='tc_a'"
        ).fetchone()[0]
        result = inc.assign_embeddings(conn, [self._row("e_hit", _blob_vector(0))])
        assert result == {"assigned": 1, "pooled": 0}
        after_row = conn.execute(
            "SELECT centroid_vector, member_count FROM topic_clusters WHERE cluster_id='tc_a'"
        ).fetchone()
        assert after_row[0] == before, "centroid must not move outside consolidation"
        assert after_row[1] == 11

    def test_threshold_env_override(self, conn, monkeypatch):
        monkeypatch.setenv("TOPOS_CLUSTER_ASSIGN_THRESHOLD", "0.5")
        v = [0.0] * 8
        v[0] = 1.0
        v[1] = 1.0
        norm = math.sqrt(2)
        result = inc.assign_embeddings(conn, [self._row("e_mid2", [x / norm for x in v])])
        assert result == {"assigned": 1, "pooled": 0}


class TestQualityMetrics:
    def test_compute_cluster_metrics_flags_mega_cluster(self):
        clusters = [
            {"member_count": 90, "members": [], "centroid_vector": None},
            {"member_count": 5, "members": [], "centroid_vector": None},
            {"member_count": 5, "members": [], "centroid_vector": None},
        ]
        metrics = tc.compute_cluster_metrics(clusters, records_loaded=100)
        assert metrics["max_cluster_share"] == 0.9
        assert metrics["clusters"] == 3

    def test_log_recompute_persists_metrics(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE cluster_recompute_log (
                recompute_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, records_processed INTEGER, clusters_written INTEGER,
                ids_preserved INTEGER, created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        inc.log_recompute(
            conn,
            kind="full",
            records_processed=100,
            clusters_written=12,
            ids_preserved=10,
            metrics={"max_cluster_share": 0.2},
        )
        row = conn.execute(
            "SELECT metrics_json FROM cluster_recompute_log"
        ).fetchone()
        assert json.loads(row[0])["max_cluster_share"] == 0.2
        conn.close()


class TestLLMLabels:
    def _cluster(self) -> dict:
        return {
            "cluster_id": "tc_x",
            "label": "training / marathon / plan",
            "label_terms": ["nutrition", "pace"],
            "members": [
                {"text_preview": "16 mile long run felt strong"},
                {"text_preview": "carb loading strategy for race week"},
            ],
            "metadata": {},
        }

    def test_labels_applied_and_term_label_preserved(self):
        cluster = self._cluster()
        count = apply_llm_cluster_labels(
            [cluster], complete=lambda prompt: "Marathon Training\n", mode="on"
        )
        assert count == 1
        assert cluster["label"] == "Marathon Training"
        assert cluster["metadata"]["term_label"] == "training / marathon / plan"

    def test_model_failure_keeps_term_labels(self):
        def _boom(prompt):
            raise RuntimeError("ollama down")

        cluster = self._cluster()
        count = apply_llm_cluster_labels([cluster], complete=_boom, mode="on")
        assert count == 0
        assert cluster["label"] == "training / marathon / plan"

    def test_first_failure_aborts_remaining_calls(self):
        calls = []

        def _boom(prompt):
            calls.append(1)
            raise RuntimeError("down")

        clusters = [self._cluster() for _ in range(5)]
        apply_llm_cluster_labels(clusters, complete=_boom, mode="on")
        assert len(calls) == 1

    def test_mode_off_never_calls_model(self):
        calls = []
        apply_llm_cluster_labels(
            [self._cluster()], complete=lambda p: calls.append(1) or "X", mode="off"
        )
        assert not calls

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Marathon Training", "Marathon Training"),
            ('"Race Prep"\nExtra text', "Race Prep"),
            ("  topos launch notes.  ", "topos launch notes"),
            ("", None),
            ("As an AI, I cannot name this", None),
            ("x" * 80, None),
            ("one two three four five six seven eight", None),
        ],
    )
    def test_parse_label(self, raw, expected):
        assert parse_label(raw) == expected

    def test_prompt_contains_terms_and_previews(self):
        prompt = build_label_prompt(self._cluster())
        assert "nutrition" in prompt
        assert "carb loading" in prompt
        assert "Label:" in prompt
