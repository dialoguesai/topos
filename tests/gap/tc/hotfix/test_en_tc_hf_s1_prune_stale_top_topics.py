"""Gap: TC-HF-S1 — prune stale top_topics facts (GT-TC-HF-S1-04)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.topic_clustering import (
    MVP_QUERY_SOURCE_IDS,
    recompute_topic_clusters,
    write_top_topics_signal_facts,
)
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def test_prune_stale_top_topics_after_recompute(tmp_path) -> None:
    """GT-TC-HF-S1-04: orphan top_topics facts removed after cluster replace."""
    conn = sqlite3.connect(str(tmp_path / "prune.db"))
    apply_all_migrations(conn)

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

    recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)
    bundle = AdapterFactory.create("local_database", conn=conn)
    write_top_topics_signal_facts(bundle, conn)

    old_fact_ids = {
        row[0]
        for row in conn.execute(
            "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
        ).fetchall()
    }
    assert old_fact_ids

    recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)
    write_top_topics_signal_facts(bundle, conn)

    new_fact_ids = {
        row[0]
        for row in conn.execute(
            "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
        ).fetchall()
    }
    cluster_ids = {
        row[0] for row in conn.execute("SELECT cluster_id FROM topic_clusters").fetchall()
    }

    assert len(new_fact_ids) == len(cluster_ids)
    for fact_id, record_id in conn.execute(
        "SELECT fact_id, record_id FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
    ).fetchall():
        assert str(record_id) in cluster_ids

    orphan_count = conn.execute(
        """
        SELECT COUNT(*) FROM signal_facts
        WHERE fact_id LIKE 'top_topics:%'
          AND record_id NOT IN (SELECT cluster_id FROM topic_clusters)
        """
    ).fetchone()[0]
    assert orphan_count == 0
    conn.close()
