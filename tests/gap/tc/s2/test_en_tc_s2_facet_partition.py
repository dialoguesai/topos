"""Gap: TC-S2 — facet-first clustering."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.topic_clustering import MVP_QUERY_SOURCE_IDS, recompute_topic_clusters
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def test_facet_partition_produces_memory_and_interests(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "facets.db"))
    apply_all_migrations(conn)
    rows = [
        ("m1", "chatgpt_file_ingestion", "memory", _vec(1, 0), "git commit signal"),
        ("m2", "chatgpt_file_ingestion", "memory", _vec(0.95, 0.05), "git pull request"),
        ("m3", "chatgpt_file_ingestion", "memory", _vec(0.9, 0.1), "github workflow"),
        ("b1", "browser_visits", "interests", _vec(0, 1), "arxiv edtech research"),
        ("b2", "browser_visits", "interests", _vec(0.05, 0.95), "hiking trails outdoors"),
        ("b3", "browser_visits", "interests", _vec(0.1, 0.9), "edtech startup news"),
    ]
    for idx, (rid, src, dim, vec, preview) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, ?, 'test', 'test', ?, ?, '{}', ?)
            """,
            (f"emb-{idx}", rid, src, dim, len(vec), preview, json.dumps(vec).encode()),
        )
    conn.commit()

    result = recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=3, k=2)
    assert result["status"] == "completed"
    dimensions = {
        row[0] for row in conn.execute("SELECT DISTINCT dimension FROM topic_clusters").fetchall()
    }
    assert "memory" in dimensions
    assert "interests" in dimensions
    conn.close()
