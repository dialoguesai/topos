"""
Gap: Topic clusters — none → memory_topic_map + top_topics for ChatGPT + browser
Sprint: EN-P2-S2 (topic clustering MVP)
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from topos.features.signal.topic_clustering import (
    MVP_QUERY_SOURCE_IDS,
    cluster_embedding_records,
    label_cluster,
    load_embedding_records,
    persist_topic_clusters,
    recompute_topic_clusters,
    write_top_topics_signal_facts,
)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def test_cluster_embedding_records_groups_similar_vectors() -> None:
    records = [
        {"record_id": "a1", "source_id": "chatgpt_file_ingestion", "vector": _vec(1.0, 0.0), "text_preview": "python code"},
        {"record_id": "a2", "source_id": "chatgpt_file_ingestion", "vector": _vec(0.95, 0.05), "text_preview": "rust code"},
        {"record_id": "b1", "source_id": "browser_visits", "vector": _vec(0.0, 1.0), "text_preview": "news site"},
        {"record_id": "b2", "source_id": "browser_visits", "vector": _vec(0.05, 0.95), "text_preview": "blog post"},
    ]
    clusters = cluster_embedding_records(records, k=2)
    assert len(clusters) == 2
    by_id = {c["cluster_id"]: {m["record_id"] for m in c["members"]} for c in clusters}
    ids = list(by_id.values())
    assert {"a1", "a2"} in ids
    assert {"b1", "b2"} in ids


def test_label_cluster_prefers_dominant_terms() -> None:
    members = [
        {"text_preview": "deploy kubernetes cluster", "metadata": {"url_category": "technology"}},
        {"text_preview": "fix docker compose", "metadata": {"url_category": "technology"}},
    ]
    label = label_cluster(members)
    assert "technology" in label.lower() or "deploy" in label.lower() or "docker" in label.lower()


def test_recompute_persists_clusters_and_top_topics(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "clusters.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)

    for idx, (rid, src, vec, preview) in enumerate(
        [
            ("m1", "chatgpt_file_ingestion", _vec(1, 0), "LLM fine-tuning"),
            ("m2", "chatgpt_file_ingestion", _vec(0.9, 0.1), "model training"),
            ("m3", "browser_visits", _vec(0, 1), "NYTimes politics"),
            ("m4", "browser_visits", _vec(0.1, 0.9), "BBC world news"),
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

    loaded = load_embedding_records(conn, source_ids=MVP_QUERY_SOURCE_IDS)
    assert len(loaded) == 4

    result = recompute_topic_clusters(conn, source_ids=MVP_QUERY_SOURCE_IDS, min_records=2, k=2)
    assert result["clusters_written"] == 2
    assert result["members_written"] == 4

    cluster_count = conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0]
    member_count = conn.execute("SELECT COUNT(*) FROM topic_cluster_members").fetchone()[0]
    assert cluster_count == 2
    assert member_count == 4

    from topos.storage.adapters.factory import AdapterFactory

    bundle = AdapterFactory.create("local_database", conn=conn)
    top_written = write_top_topics_signal_facts(bundle, conn, limit=10)
    assert top_written >= 2

    top_facts = conn.execute(
        "SELECT payload_json FROM signal_facts"
    ).fetchall()
    assert len(top_facts) >= 2
    assert any("top_topics" in (row[0] or "") for row in top_facts)
    conn.close()


def test_mvp_query_source_ids_cover_chatgpt_and_browser() -> None:
    from topos.sources.registry import topic_cluster_source_ids

    configured = topic_cluster_source_ids()
    assert "chatgpt_file_ingestion" in configured
    assert "chatgpt_ui_conversation" in configured
    assert "browser_visits" in configured
    assert "demo_journal_file" in configured


def test_time_log_embeddings_included_in_cluster_scope(tmp_path) -> None:
    from topos.sources.registry import REGISTRY
    from topos.sources.runtime_install import install_source_definition

    handle = install_source_definition(
        {
            "source_id": "time_log",
            "display_name": "Time Log",
            "source_type": "ui_stream",
            "schema_id": "journal.time_log.v1",
            "parser_id": "journal.time_log.v1",
            "canonical_mapper_id": "journal_time_log",
            "canonical_group_id": "journal",
            "pipeline_include_data_table": True,
            "tables": [
                {
                    "table_id": "time_log_sessions",
                    "display_name": "Sessions",
                    "columns": [{"name": "record_id", "type": "text", "primary_key": True}],
                }
            ],
        }
    )
    try:
        conn = sqlite3.connect(str(tmp_path / "time-log-clusters.db"))
        apply_all_migrations(conn)
        conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, 'memory', 'test', 'test', ?, ?, '{}', ?)
            """,
            ("emb-tl-1", "tl-1", "time_log", 2, "Ship feature", json.dumps(_vec(1, 0)).encode("utf-8")),
        )
        conn.commit()

        loaded = load_embedding_records(conn, source_ids=["time_log"])
        assert len(loaded) == 1
        assert loaded[0]["record_type"] == "journal_entry"
    finally:
        handle.uninstall()
        assert "time_log" not in REGISTRY


def test_persist_topic_clusters_idempotent_replace(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "persist.db"))
    apply_all_migrations(conn)
    batch = str(uuid.uuid4())
    clusters = [
        {
            "cluster_id": "tc_test_1",
            "label": "Tech",
            "dimension": "memory",
            "member_count": 1,
            "source_mix": {"chatgpt_file_ingestion": 1},
            "label_terms": ["tech"],
            "centroid_preview": "code",
            "members": [
                {
                    "record_id": "m1",
                    "source_id": "chatgpt_file_ingestion",
                    "record_type": "ai_chat_message",
                    "text_preview": "code",
                    "weight": 1.0,
                }
            ],
        }
    ]
    n1 = persist_topic_clusters(conn, clusters, sync_batch_id=batch)
    n2 = persist_topic_clusters(conn, clusters, sync_batch_id=batch)
    assert n1["clusters_written"] == 1
    assert n2["clusters_written"] == 1
    assert conn.execute("SELECT COUNT(*) FROM topic_clusters").fetchone()[0] == 1
    conn.close()
