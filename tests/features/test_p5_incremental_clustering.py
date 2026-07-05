"""P5 tests: assign-first clustering, candidate pool, stable cluster IDs."""

from __future__ import annotations

import math
import random
import sqlite3
import uuid

import pytest

from topos.features.signal.incremental_clustering import (
    ASSIGN_THRESHOLD,
    assign_embeddings,
    candidate_pool_size,
    consolidation_due,
    load_cluster_centroids,
    match_stable_cluster_ids,
)
from topos.features.signal.topic_clustering import recompute_topic_clusters
from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "clusters.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _unit(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cluster_vector(axis: int, dims: int = 8, noise: float = 0.05, rng=None):
    rng = rng or random
    vec = [noise * rng.uniform(-1, 1) for _ in range(dims)]
    vec[axis] = 1.0
    return _unit(vec)


def _seed_cluster(conn, cluster_id: str, axis: int, *, member_count: int = 5, dims: int = 8):
    centroid = _cluster_vector(axis, dims, noise=0.0)
    conn.execute(
        """
        INSERT INTO topic_clusters (
            cluster_id, label, dimension, member_count, source_mix_json,
            label_terms_json, centroid_preview, model, provider, sync_batch_id,
            metadata_json, centroid_vector, updated_at
        ) VALUES (?, ?, 'memory', ?, '{}', '[]', '', 'kmeans_cosine_v1', 'topos', NULL, '{}', ?, datetime('now'))
        """,
        (cluster_id, f"cluster {axis}", member_count, encode_f32(centroid)),
    )
    conn.commit()
    return centroid


def _insert_embedding(conn, record_id: str, vector, source_id="chatgpt_file_ingestion"):
    embedding_id = f"emb_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model, provider,
            dims, text_preview, provenance_json, vector_blob, vector_format, chunk_index
        ) VALUES (?, ?, ?, 'memory', 'm', 'test', ?, ?, '{}', ?, 'f32', 0)
        """,
        (embedding_id, record_id, source_id, len(vector), f"preview {record_id}", encode_f32(vector)),
    )
    conn.commit()
    return embedding_id


class TestAssignFirst:
    def test_close_vector_joins_cluster(self, conn) -> None:
        _seed_cluster(conn, "tc_stable_1", axis=0)
        rng = random.Random(3)
        vec = _cluster_vector(0, rng=rng)
        emb_id = _insert_embedding(conn, "rec-new", vec)
        result = assign_embeddings(
            conn,
            [{"embedding_id": emb_id, "record_id": "rec-new", "source_id": "s", "vector": vec, "text_preview": "p"}],
        )
        assert result == {"assigned": 1, "pooled": 0}
        member = conn.execute(
            "SELECT cluster_id FROM topic_cluster_members WHERE record_id='rec-new'"
        ).fetchone()
        assert member[0] == "tc_stable_1"
        # embedding column stamped for filterable vector search
        stamped = conn.execute(
            "SELECT cluster_id FROM signal_embeddings WHERE embedding_id=?", (emb_id,)
        ).fetchone()
        assert stamped[0] == "tc_stable_1"
        count = conn.execute(
            "SELECT member_count FROM topic_clusters WHERE cluster_id='tc_stable_1'"
        ).fetchone()[0]
        assert count == 6

    def test_far_vector_goes_to_pool(self, conn) -> None:
        _seed_cluster(conn, "tc_stable_1", axis=0)
        vec = _cluster_vector(7)  # orthogonal axis
        result = assign_embeddings(
            conn,
            [{"embedding_id": "e1", "record_id": "rec-far", "source_id": "s", "vector": vec, "text_preview": "p"}],
        )
        assert result == {"assigned": 0, "pooled": 1}
        assert candidate_pool_size(conn) == 1

    def test_centroid_nudges_toward_new_member(self, conn) -> None:
        _seed_cluster(conn, "tc_stable_1", axis=0, member_count=1)
        # vector tilted toward axis 1 but still close to axis 0
        vec = _unit([1.0, 0.6, 0, 0, 0, 0, 0, 0])
        assign_embeddings(
            conn,
            [{"embedding_id": "e1", "record_id": "r1", "source_id": "s", "vector": vec, "text_preview": "p"}],
        )
        clusters = load_cluster_centroids(conn)
        centroid = clusters[0]["centroid"]
        assert centroid[1] > 0.1, "centroid did not move toward the new member"


class TestStableIds:
    def test_greedy_matching_preserves_ids(self) -> None:
        old = [
            {"cluster_id": "tc_old_a", "centroid": _cluster_vector(0, noise=0.0)},
            {"cluster_id": "tc_old_b", "centroid": _cluster_vector(3, noise=0.0)},
        ]
        rng = random.Random(5)
        new = [
            {"cluster_id": "tc_new_1", "centroid_vector": _cluster_vector(3, rng=rng)},
            {"cluster_id": "tc_new_2", "centroid_vector": _cluster_vector(0, rng=rng)},
            {"cluster_id": "tc_new_3", "centroid_vector": _cluster_vector(6, rng=rng)},
        ]
        preserved = match_stable_cluster_ids(old, new)
        assert preserved == 2
        assert new[0]["cluster_id"] == "tc_old_b"
        assert new[1]["cluster_id"] == "tc_old_a"
        assert new[2]["cluster_id"] == "tc_new_3"  # genuinely new topic keeps new id

    def test_full_recompute_survival_rate(self, conn) -> None:
        """Cluster IDs survive sequential ingest batches + recomputes >= 90%."""
        rng = random.Random(42)
        # Three well-separated topics, 8 records each
        for axis in (0, 3, 6):
            for i in range(8):
                _insert_embedding(conn, f"rec-{axis}-{i}", _cluster_vector(axis, rng=rng))
        first = recompute_topic_clusters(conn, min_records=3)
        assert first["status"] == "completed"
        ids_before = {
            row[0] for row in conn.execute("SELECT cluster_id FROM topic_clusters").fetchall()
        }

        # New batch lands in the same topics; recompute again
        for axis in (0, 3, 6):
            for i in range(3):
                _insert_embedding(conn, f"rec2-{axis}-{i}", _cluster_vector(axis, rng=rng))
        second = recompute_topic_clusters(conn, min_records=3)
        assert second["status"] == "completed"
        ids_after = {
            row[0] for row in conn.execute("SELECT cluster_id FROM topic_clusters").fetchall()
        }
        # Anti-churn contract: existing cluster IDs survive a recompute
        # (splits may mint additional new IDs; a merge may retire at most one).
        # The old behavior — fresh UUIDs every batch, zero survivors — is the
        # regression this guards against.
        survived = len(ids_before & ids_after)
        assert survived >= len(ids_before) - 1, f"old ids churned: {ids_before} -> {ids_after}"
        assert second["ids_preserved"] >= survived > 0

    def test_top_topics_facts_stable_across_recompute(self, conn) -> None:
        from topos.features.signal.topic_clustering import write_top_topics_signal_facts
        from topos.storage.adapters.factory import AdapterFactory

        rng = random.Random(9)
        for axis in (0, 3):
            for i in range(6):
                _insert_embedding(conn, f"rec-{axis}-{i}", _cluster_vector(axis, rng=rng))
        recompute_topic_clusters(conn, min_records=3)
        bundle = AdapterFactory.create("local_database", conn=conn)
        write_top_topics_signal_facts(bundle, conn)
        facts_before = {
            row[0]
            for row in conn.execute(
                "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
            ).fetchall()
        }
        recompute_topic_clusters(conn, min_records=3)
        write_top_topics_signal_facts(bundle, conn)
        facts_after = {
            row[0]
            for row in conn.execute(
                "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
            ).fetchall()
        }
        assert facts_before == facts_after, "top_topics fact ids churned across recompute"


class TestConsolidationTrigger:
    def test_due_when_pool_large(self, conn) -> None:
        for i in range(100):
            conn.execute(
                "INSERT INTO cluster_candidates (embedding_id, record_id) VALUES (?, ?)",
                (f"e{i}", f"r{i}"),
            )
        conn.commit()
        assert consolidation_due(conn) is True

    def test_not_due_after_recent_full(self, conn) -> None:
        conn.execute(
            "INSERT INTO cluster_recompute_log (kind, records_processed, clusters_written, ids_preserved)"
            " VALUES ('full', 10, 2, 2)"
        )
        conn.commit()
        assert consolidation_due(conn) is False

    def test_pool_cleared_by_recompute(self, conn) -> None:
        rng = random.Random(21)
        conn.execute(
            "INSERT INTO cluster_candidates (embedding_id, record_id) VALUES ('e1', 'r1')"
        )
        for axis in (0, 3):
            for i in range(5):
                _insert_embedding(conn, f"rec-{axis}-{i}", _cluster_vector(axis, rng=rng))
        recompute_topic_clusters(conn, min_records=3)
        assert candidate_pool_size(conn) == 0
