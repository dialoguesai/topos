"""Gap: TC-HF-S1 — idempotent topic cluster recompute (GT-TC-HF-S1-01, GT-TC-HF-S1-02)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.topic_clustering import MVP_QUERY_SOURCE_IDS, recompute_topic_clusters
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def _seed_embeddings(conn: sqlite3.Connection) -> None:
    rows = [
        ("m1", "chatgpt_file_ingestion", _vec(1, 0), "kubernetes deploy docker"),
        ("m2", "chatgpt_file_ingestion", _vec(0.95, 0.05), "docker compose nginx"),
        ("m3", "chatgpt_file_ingestion", _vec(0.9, 0.1), "helm chart kubernetes"),
        ("m4", "browser_visits", _vec(0, 1), "politics election news"),
        ("m5", "browser_visits", _vec(0.05, 0.95), "world news politics"),
        ("m6", "browser_visits", _vec(0.1, 0.9), "election coverage"),
    ]
    for idx, (rid, src, vec, preview) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, 'memory', 'test', 'test', ?, ?, '{}', ?)
            """,
            (f"emb-{idx}", rid, src, len(vec), preview, json.dumps(vec).encode("utf-8")),
        )
    conn.commit()


def test_recompute_three_times_stable_count(tmp_path) -> None:
    """GT-TC-HF-S1-01: cluster count stable across 3 recomputes."""
    conn = sqlite3.connect(str(tmp_path / "idempotent.db"))
    apply_all_migrations(conn)
    _seed_embeddings(conn)

    counts: list[int] = []
    for _ in range(3):
        result = recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=3, k=2)
        assert result["status"] == "completed"
        counts.append(conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0])

    assert len(set(counts)) == 1
    assert counts[0] == 2
    conn.close()


def test_recompute_no_duplicate_labels_across_runs(tmp_path) -> None:
    """GT-TC-HF-S1-02: no duplicate labels after recompute."""
    conn = sqlite3.connect(str(tmp_path / "labels.db"))
    apply_all_migrations(conn)
    _seed_embeddings(conn)

    for _ in range(3):
        recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=3, k=2)

    dupes = conn.execute(
        """
        SELECT label, COUNT(*) AS n
        FROM topic_clusters
        GROUP BY label
        HAVING n > 1
        """
    ).fetchall()
    assert dupes == []
    conn.close()
