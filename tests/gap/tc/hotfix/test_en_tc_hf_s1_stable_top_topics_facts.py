"""Gap: TC-HF-S1 — stable top_topics signal facts (GT-TC-HF-S1-03)."""

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


def _seed_and_recompute(conn: sqlite3.Connection) -> None:
    for idx, (rid, src, vec, preview) in enumerate(
        [
            ("m1", "chatgpt_file_ingestion", _vec(1, 0), "alpha"),
            ("m2", "chatgpt_file_ingestion", _vec(0.9, 0.1), "beta"),
            ("m3", "browser_visits", _vec(0, 1), "gamma"),
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


def test_top_topics_fact_count_stable_after_two_writes(tmp_path) -> None:
    """GT-TC-HF-S1-03: write_top_topics twice does not double fact rows."""
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    apply_all_migrations(conn)
    _seed_and_recompute(conn)

    bundle = AdapterFactory.create("local_database", conn=conn)
    cluster_count = conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0]

    write_top_topics_signal_facts(bundle, conn)
    count_after_first = conn.execute(
        "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
    ).fetchone()[0]

    write_top_topics_signal_facts(bundle, conn)
    count_after_second = conn.execute(
        "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
    ).fetchone()[0]

    assert count_after_first == cluster_count
    assert count_after_second == cluster_count
    conn.close()


def test_top_topics_fact_count_stable_after_two_recomputes(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "recompute_facts.db"))
    apply_all_migrations(conn)
    _seed_and_recompute(conn)

    bundle = AdapterFactory.create("local_database", conn=conn)
    write_top_topics_signal_facts(bundle, conn)

    recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)
    write_top_topics_signal_facts(bundle, conn)

    cluster_count = conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0]
    fact_count = conn.execute(
        "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'top_topics:%'"
    ).fetchone()[0]
    assert fact_count == cluster_count
    conn.close()
