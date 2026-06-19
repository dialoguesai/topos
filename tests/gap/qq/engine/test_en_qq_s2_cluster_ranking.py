"""GT-EN-QQ-S2-03: Query-ranked topic clusters."""

import sqlite3

import pytest

from topos.features.signal.topic_clustering import rank_topic_clusters_for_query
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


@pytest.fixture
def clusters():
    return [
        {
            "cluster_id": "docker",
            "label": "docker / compose / nginx",
            "label_terms": ["docker", "compose", "nginx"],
            "member_count": 40,
        },
        {
            "cluster_id": "art",
            "label": "prompt / illustration / here",
            "label_terms": ["prompt", "illustration", "pencil"],
            "member_count": 8,
        },
    ]


def test_rank_topic_clusters_for_query_gt_en_qq_s2_03(clusters) -> None:
    ranked = rank_topic_clusters_for_query(clusters, "illustration pencil", limit=5)
    assert ranked[0]["cluster_id"] == "art"
    assert ranked[0]["relevance_score"] >= ranked[1]["relevance_score"]


def test_load_clusters_from_db(tmp_path, monkeypatch, clusters) -> None:
    db_path = tmp_path / "clusters.db"
    conn = sqlite3.connect(str(db_path))
    apply_all_migrations(conn)
    for cluster in clusters:
        conn.execute(
            """
            INSERT INTO topic_clusters (
                cluster_id, label, dimension, member_count, source_mix_json, label_terms_json, centroid_preview
            ) VALUES (?, ?, 'memory', ?, '{}', ?, '')
            """,
            (
                cluster["cluster_id"],
                cluster["label"],
                cluster["member_count"],
                '["' + '","'.join(cluster["label_terms"]) + '"]',
            ),
        )
    conn.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    from topos.features.signal.topic_clustering import load_topic_clusters_for_query

    loaded = load_topic_clusters_for_query(conn, limit=10)
    ranked = rank_topic_clusters_for_query(loaded, "docker nginx", limit=2)
    assert ranked[0]["cluster_id"] == "docker"
    conn.close()
