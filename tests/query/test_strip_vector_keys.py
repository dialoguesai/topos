"""§3.6 — no vector-shaped key reaches a context packet.

The gate is name-based on purpose: a *null* centroid is still a contract
violation, because the cross-user no-raw-vectors check scans key shapes rather
than payload contents. Mention-context centroids added a family of names
(``centroid_blob``, ``*_centroid``) that the original patterns did not cover.
"""

from __future__ import annotations

import pytest

from topos.query.retrieval import _strip_vector_keys

pytestmark = pytest.mark.public


@pytest.mark.parametrize(
    "key",
    [
        "embedding",
        "vector",
        "embedding_blob",
        "centroid_vector",
        "query_vector",
        "centroid",
        "centroid_blob",
        "context_centroid",
        "entity_centroid",
    ],
)
def test_vector_shaped_keys_are_dropped(key: str) -> None:
    assert _strip_vector_keys({key: [0.1, 0.2], "label": "work"}) == {"label": "work"}


@pytest.mark.parametrize("key", ["centroid_blob", "context_centroid"])
def test_null_centroid_keys_are_dropped_too(key: str) -> None:
    assert key not in _strip_vector_keys({key: None, "label": "work"})


def test_descriptive_keys_survive() -> None:
    item = {
        "cluster_id": "c1",
        "label": "work",
        "member_count": 12,
        "relevance_score": 0.4,
        "centroid_distance": 0.2,
    }
    assert _strip_vector_keys(item) == item
