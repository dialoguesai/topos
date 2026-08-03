"""Affinity quality scorecard — automated contamination + diffable snapshots."""

from __future__ import annotations

import json
import math
import sqlite3

import pytest

from topos.features.entities.affinity_quality import (
    build_affinity_quality_snapshot,
    compare_snapshots,
    score_affinity_pairs,
)
from topos.features.entities.edges import EDGE_SEMANTIC_AFFINITY
from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

_DIMS = 8


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "affinity_quality.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _unit(components: dict) -> list:
    vector = [0.0] * _DIMS
    for axis, value in components.items():
        vector[axis] = value
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _add_person(conn, entity_id: str, name: str) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,
                              mention_count, is_self)
        VALUES (?, 'person', ?, ?, 6, 0)
        """,
        (entity_id, name, name.lower()),
    )


def _add_centroid(conn, entity_id: str, vector) -> None:
    conn.execute(
        """
        INSERT INTO entity_context_vectors
            (entity_id, centroid_blob, mention_sample, source_sample,
             model_name, computed_at)
        VALUES (?, ?, 6, 3, 'test-model', '2026-08-03T00:00:00Z')
        """,
        (entity_id, encode_f32(vector)),
    )


def _add_affinity(conn, a: str, b: str, weight: float = 0.8) -> None:
    src, dst = (a, b) if a < b else (b, a)
    conn.execute(
        """
        INSERT INTO entity_edges
            (edge_id, src_entity_id, dst_entity_id, edge_type,
             weight, evidence_count, valid_from)
        VALUES (?, ?, ?, ?, ?, 3, '2026-08-01T00:00:00Z')
        """,
        (f"edg-{src}-{dst}", src, dst, EDGE_SEMANTIC_AFFINITY, weight),
    )


def _add_mention(conn, entity_id: str, record_id: str, *, content_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO entity_mentions (mention_id, entity_id, record_id, surface_text)
        VALUES (?, ?, ?, ?)
        """,
        (f"m-{entity_id}-{record_id}", entity_id, record_id, entity_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_embeddings
            (embedding_id, record_id, vector_blob, vector_format, dims, model,
             content_hash)
        VALUES (?, ?, ?, 'f32', ?, 'test-model', ?)
        """,
        (
            f"e-{record_id}",
            record_id,
            encode_f32(_unit({0: 1.0})),
            _DIMS,
            content_hash,
        ),
    )


def test_empty_node_reports_withhold_status(conn) -> None:
    snap = build_affinity_quality_snapshot(conn)

    assert snap["status"] == "empty_edge_set"
    assert snap["pair_metrics"]["active_edges"] == 0
    assert snap["honest_claims"]["can_claim_mechanism_withhold"] is True
    assert snap["honest_claims"]["can_claim_quality_improved"] is False
    assert snap["derived_spec"]["live"]
    assert snap["scorecard_version"] == 2
    pop = snap["population"]
    assert pop["centroid_coverage_of_all_people"] is None or pop[
        "centroid_coverage_of_all_people"
    ] == 0.0 or pop["person_entities_non_self"] == 0
    assert "centroid_coverage_of_significant" in pop
    assert "centroid_coverage_of_eligible" in pop


def test_coverage_uses_significant_not_all_people(conn) -> None:
    """NER residue must not dominate the primary coverage ratio."""
    from topos.features.entities.context_vectors import MIN_CONTEXT_MENTIONS

    # One significant person with a centroid…
    _add_person(conn, "a", "Ana")
    conn.execute(
        "UPDATE entities SET mention_count=? WHERE entity_id='a'",
        (MIN_CONTEXT_MENTIONS,),
    )
    _add_centroid(conn, "a", _unit({0: 1.0}))
    # …and a long tail of never-mentioned person rows.
    for i in range(50):
        _add_person(conn, f"noise-{i}", f"Noise {i}")
        conn.execute(
            "UPDATE entities SET mention_count=0 WHERE entity_id=?",
            (f"noise-{i}",),
        )
    conn.commit()

    pop = build_affinity_quality_snapshot(conn)["population"]

    assert pop["context_centroids"] == 1
    assert pop["people_significant"] == 1
    assert pop["centroid_coverage_of_significant"] == 1.0
    assert pop["centroid_coverage"] == 1.0  # alias → significant
    assert pop["centroid_coverage_of_all_people"] == pytest.approx(1 / 51, abs=1e-3)
    assert pop["centroid_coverage_of_all_people"] < 0.05


def test_alias_collision_is_flagged(conn) -> None:
    _add_person(conn, "sarah", "Sarah Chen")
    _add_person(conn, "sara", "Sara")
    _add_centroid(conn, "sarah", _unit({0: 1.0}))
    _add_centroid(conn, "sara", _unit({0: 0.99, 1: 0.1}))
    _add_affinity(conn, "sarah", "sara", 0.91)
    conn.execute(
        """
        INSERT INTO entity_review
            (review_id, surface_text, kind, subject_entity_id,
             candidate_entity_id, status, score)
        VALUES ('r1', 'Sara', 'merge', 'sarah', 'sara', 'pending', 0.95)
        """
    )
    conn.commit()

    metrics = score_affinity_pairs(conn, [
        {
            "src_entity_id": "sara",
            "dst_entity_id": "sarah",
            "src_name": "Sara",
            "dst_name": "Sarah Chen",
            "weight": 0.91,
        }
    ])

    assert metrics["active_edges"] == 1
    assert metrics["alias_collision_count"] == 1
    assert metrics["alias_collision_rate"] == 1.0

    snap = build_affinity_quality_snapshot(conn)
    assert snap["status"] == "contamination_present"


def test_shared_source_and_co_occurrence_residual(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    _add_centroid(conn, "a", _unit({0: 1.0}))
    _add_centroid(conn, "b", _unit({1: 1.0}))
    _add_affinity(conn, "a", "b", 0.7)
    # Same source document for both — the §3.1a clique shape.
    _add_mention(conn, "a", "rec-1", content_hash="page-x")
    _add_mention(conn, "b", "rec-2", content_hash="page-x")
    conn.execute(
        """
        INSERT INTO entity_edges
            (edge_id, src_entity_id, dst_entity_id, edge_type,
             weight, evidence_count, valid_from)
        VALUES ('edg-co', 'a', 'b', 'co_occurrence', 2.0, 2, '2026-08-01T00:00:00Z')
        """
    )
    conn.commit()

    metrics = score_affinity_pairs(
        conn,
        [
            {
                "src_entity_id": "a",
                "dst_entity_id": "b",
                "src_name": "Ana",
                "dst_name": "Bo",
                "weight": 0.7,
            }
        ],
        merge_pairs=set(),
    )

    assert metrics["shared_source_count"] == 1
    assert metrics["co_occurrence_residual_count"] == 1


def test_near_identical_centroids_flagged(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    vec = _unit({0: 1.0})
    _add_centroid(conn, "a", vec)
    _add_centroid(conn, "b", vec)
    _add_affinity(conn, "a", "b", 1.0)
    conn.commit()

    snap = build_affinity_quality_snapshot(conn)

    assert snap["pair_metrics"]["near_identity_count"] == 1
    assert snap["status"] == "contamination_present"


def test_compare_snapshots_jaccard_and_deltas(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    _add_person(conn, "c", "Cy")
    _add_centroid(conn, "a", _unit({0: 1.0}))
    _add_centroid(conn, "b", _unit({1: 1.0}))
    _add_centroid(conn, "c", _unit({2: 1.0}))
    _add_affinity(conn, "a", "b", 0.8)
    conn.commit()

    first = build_affinity_quality_snapshot(conn)
    _add_affinity(conn, "a", "c", 0.75)
    conn.commit()
    second = build_affinity_quality_snapshot(conn)

    diff = compare_snapshots(second, first)
    assert diff["compared_to"] == first["ts"]
    assert diff["active_edges_delta"] == 1.0
    assert diff["edge_set_jaccard"] == pytest.approx(0.5)


def test_snapshot_is_json_serialisable(conn) -> None:
    _add_person(conn, "a", "Ana")
    conn.commit()
    snap = build_affinity_quality_snapshot(conn)
    blob = json.dumps(snap, default=str)
    assert "scorecard_version" in blob
