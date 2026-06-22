"""Gap: TC-HF-S1 — transaction-safe recompute (GT-TC-HF-S1-05)."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from topos.features.signal.topic_clustering import MVP_QUERY_SOURCE_IDS, recompute_topic_clusters
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def _seed(conn: sqlite3.Connection) -> None:
    for idx, (rid, src, vec, preview) in enumerate(
        [
            ("m1", "chatgpt_file_ingestion", _vec(1, 0), "one"),
            ("m2", "chatgpt_file_ingestion", _vec(0.9, 0.1), "two"),
            ("m3", "browser_visits", _vec(0, 1), "three"),
        ]
    ):
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


def test_recompute_rollback_preserves_prior_on_failure(tmp_path) -> None:
    """GT-TC-HF-S1-05: failed persist leaves prior snapshot intact."""
    conn = sqlite3.connect(str(tmp_path / "rollback.db"))
    apply_all_migrations(conn)
    _seed(conn)

    recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)
    prior_count = conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0]
    prior_labels = {
        row[0] for row in conn.execute("SELECT label FROM topic_clusters").fetchall()
    }
    assert prior_count >= 1

    with patch(
        "topos.features.signal.topic_clustering.persist_topic_clusters",
        side_effect=RuntimeError("simulated persist failure"),
    ):
        with pytest.raises(RuntimeError):
            recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)

    after_count = conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0]
    after_labels = {
        row[0] for row in conn.execute("SELECT label FROM topic_clusters").fetchall()
    }
    assert after_count == prior_count
    assert after_labels == prior_labels
    conn.close()
