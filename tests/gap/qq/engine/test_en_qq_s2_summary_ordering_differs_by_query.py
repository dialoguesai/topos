"""GT-EN-QQ-S2-04: Different queries produce different top summaries."""

import sqlite3

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

from qq_helpers import ai_conversations_manifest

pytestmark = pytest.mark.gap


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "summary_rank.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    rows = [
        ("docker", "docker / compose / nginx", ["docker", "compose", "nginx"], 40),
        ("art", "prompt / illustration / here", ["prompt", "illustration", "pencil"], 8),
    ]
    for cluster_id, label, terms, count in rows:
        c.execute(
            """
            INSERT INTO topic_clusters (
                cluster_id, label, dimension, member_count, source_mix_json, label_terms_json, centroid_preview
            ) VALUES (?, ?, 'memory', ?, '{}', ?, '')
            """,
            (cluster_id, label, count, '["' + '","'.join(terms) + '"]'),
        )
    c.commit()
    yield c
    c.close()


def test_q1_vs_q5_different_first_summary(conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = ai_conversations_manifest()
    q_docker = adapter.retrieve(
        RetrievalRequest(manifest=manifest, access_mode="summary", query_text="docker nginx compose")
    )
    q_art = adapter.retrieve(
        RetrievalRequest(manifest=manifest, access_mode="summary", query_text="illustration pencil")
    )
    top_docker = (q_docker.context_packet.get("summaries") or [{}])[0]
    top_art = (q_art.context_packet.get("summaries") or [{}])[0]
    assert top_docker.get("topic") != top_art.get("topic")
    assert top_docker.get("relevance_score", 0) >= top_art.get("relevance_score", 0) or top_docker["topic"] != top_art["topic"]
