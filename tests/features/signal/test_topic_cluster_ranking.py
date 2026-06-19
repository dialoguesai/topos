"""Unit tests for query-ranked topic clusters."""

from topos.features.signal.topic_clustering import rank_topic_clusters_for_query


def test_illustration_query_ranks_illustration_cluster_first() -> None:
    clusters = [
        {
            "cluster_id": "c1",
            "label": "docker / compose / nginx",
            "label_terms": ["docker", "compose", "nginx"],
            "member_count": 50,
        },
        {
            "cluster_id": "c2",
            "label": "prompt / illustration / here",
            "label_terms": ["prompt", "illustration", "pencil"],
            "member_count": 10,
        },
    ]
    ranked = rank_topic_clusters_for_query(clusters, "illustration pencil", limit=5)
    assert ranked[0]["cluster_id"] == "c2"
    assert ranked[0]["relevance_score"] > ranked[1]["relevance_score"]


def test_empty_query_falls_back_to_member_count() -> None:
    clusters = [
        {"cluster_id": "big", "label": "alpha", "label_terms": [], "member_count": 100},
        {"cluster_id": "small", "label": "beta", "label_terms": [], "member_count": 5},
    ]
    ranked = rank_topic_clusters_for_query(clusters, "", limit=2)
    assert ranked[0]["cluster_id"] == "big"
