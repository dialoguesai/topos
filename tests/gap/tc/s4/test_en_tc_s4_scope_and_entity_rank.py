"""Gap: TC-S4 — scope filter and entity boost ranking."""

from __future__ import annotations

import pytest

from topos.features.signal.topic_clustering import (
    filter_clusters_by_dimensions,
    rank_topic_clusters_for_query,
)

pytestmark = pytest.mark.gap


def test_scope_filter_excludes_memory_for_activity_scope() -> None:
    clusters = [
        {"cluster_id": "c1", "label": "git", "primary_dimension": "memory", "member_count": 10},
        {"cluster_id": "c2", "label": "hiking", "primary_dimension": "interests", "member_count": 5},
    ]
    filtered = filter_clusters_by_dimensions(clusters, ["Interests"])
    assert len(filtered) == 1
    assert filtered[0]["cluster_id"] == "c2"


def test_entity_boost_ranks_edtech_bridge_first() -> None:
    clusters = [
        {
            "cluster_id": "docker",
            "label": "docker / nginx",
            "label_terms": ["docker"],
            "member_count": 50,
            "metadata": {"related_entities": [], "query_aliases": ["docker"]},
        },
        {
            "cluster_id": "edtech",
            "label": "edtech / bridge",
            "label_terms": ["edtech", "intro"],
            "member_count": 5,
            "metadata": {
                "related_entities": ["Marcus Webb", "Sara Chen"],
                "query_aliases": ["edtech", "marcus", "sara", "intro"],
                "opportunity_type": "network_bridge",
            },
        },
    ]
    ranked = rank_topic_clusters_for_query(
        clusters,
        "Should Jordan intro Sara to Marcus for edtech?",
        limit=2,
    )
    assert ranked[0]["cluster_id"] == "edtech"
    assert ranked[0]["relevance_score"] >= ranked[1]["relevance_score"]
