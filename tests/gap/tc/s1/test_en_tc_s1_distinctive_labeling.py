"""Gap: TC-S1 — distinctive labeling and merge gates."""

from __future__ import annotations

import pytest

from topos.features.signal.topic_clustering import (
    _finalize_cluster_labels,
    _post_process_facet_clusters,
    cluster_embedding_records,
    label_cluster,
)

pytestmark = pytest.mark.gap


def _vec(*coords: float) -> list[float]:
    return [float(x) for x in coords]


def test_distinctive_terms_differ_for_similar_clusters() -> None:
    records = []
    for i in range(7):
        records.append(
            {
                "record_id": f"d{i}",
                "source_id": "chatgpt_file_ingestion",
                "signal_dimension": "memory",
                "vector": _vec(1.0, float(i) * 0.01),
                "text_preview": "docker kubernetes deploy pipeline",
            }
        )
    for i in range(3):
        records.append(
            {
                "record_id": f"e{i}",
                "source_id": "chatgpt_file_ingestion",
                "signal_dimension": "memory",
                "vector": _vec(0.2, 1.0 - float(i) * 0.02),
                "text_preview": "edtech classroom pilots teachers",
            }
        )
    clusters = _finalize_cluster_labels(
        _post_process_facet_clusters(cluster_embedding_records(records, k=3), merge_small=False)
    )
    labels = [c["label"] for c in clusters if int(c.get("member_count") or 0) >= 3]
    assert len(set(labels)) == len(labels)


def test_label_cluster_prefers_dominant_terms() -> None:
    members = [
        {"text_preview": "deploy kubernetes cluster"},
        {"text_preview": "fix docker compose"},
    ]
    label = label_cluster(members)
    assert "technology" in label.lower() or "deploy" in label.lower() or "docker" in label.lower()
