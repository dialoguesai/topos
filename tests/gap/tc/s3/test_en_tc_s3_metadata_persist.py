"""Gap: TC-S3 — coordination metadata on clusters."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.topic_clustering import (
    MVP_QUERY_SOURCE_IDS,
    load_topic_clusters_for_query,
    recompute_topic_clusters,
)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def test_cluster_metadata_includes_opportunity_type(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    apply_all_migrations(conn)
    for idx, (rid, src, dim, vec, preview) in enumerate(
        [
            ("m1", "demo_messenger_file", "relationships", _vec(1, 0), "Sara edtech intro Marcus"),
            ("m2", "demo_messenger_file", "relationships", _vec(0.95, 0.05), "Marcus fundraising Austin"),
            ("m3", "demo_messenger_file", "relationships", _vec(0.9, 0.1), "edtech pilot intro"),
        ]
    ):
        conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, ?, 'test', 'test', ?, ?, '{}', ?)
            """,
            (f"emb-{idx}", rid, src, dim, len(vec), preview, json.dumps(vec).encode()),
        )
        conn.execute(
            """
            INSERT INTO message_entities (entity_id, record_id, source_id, entity_text, payload_json)
            VALUES (?, ?, ?, ?, '{}')
            """,
            (f"ent-{idx}-sara", rid, src, "Sara Chen"),
        )
        conn.execute(
            """
            INSERT INTO message_entities (entity_id, record_id, source_id, entity_text, payload_json)
            VALUES (?, ?, ?, ?, '{}')
            """,
            (f"ent-{idx}-marcus", rid, src, "Marcus Webb"),
        )
    conn.commit()

    result = recompute_topic_clusters(conn, source_ids=("demo_messenger_file",), min_records=3, k=1)
    assert result["status"] == "completed"
    clusters = load_topic_clusters_for_query(conn, limit=10)
    assert clusters
    assert clusters[0].get("metadata")
    assert clusters[0]["metadata"].get("opportunity_type")
    conn.close()
