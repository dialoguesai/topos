"""Owner affinity status / config / labels / diagnostics."""

from __future__ import annotations

import math
import sqlite3

import pytest

from topos.features.entities.affinity import rebuild_affinity_edges
from topos.features.entities.affinity_owner import (
    apply_affinity_config,
    get_affinity_status,
    label_affinity_pair,
    list_affinity_pairs_for_review,
    nudge_percentile,
    recompute_affinity_now,
    set_affinity_percentile,
)
from topos.features.entities.edges import EDGE_CO_OCCURRENCE, EDGE_SEMANTIC_AFFINITY
from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

_DIMS = 8


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "affinity_owner.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _unit(components: dict) -> list:
    vector = [0.0] * _DIMS
    for axis, value in components.items():
        vector[axis] = value
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _tilted(axis: int, along: float) -> list:
    return _unit({0: along, axis: math.sqrt(max(0.0, 1.0 - along * along))})


def _add_person(conn, entity_id: str, name: str, *, mentions: int = 6) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,
                              mention_count, is_self)
        VALUES (?, 'person', ?, ?, ?, 0)
        """,
        (entity_id, name, name.lower(), mentions),
    )


def _add_centroid(conn, entity_id: str, vector, *, source_sample: int = 10) -> None:
    conn.execute(
        """
        INSERT INTO entity_context_vectors
            (entity_id, centroid_blob, mention_sample, source_sample,
             model_name, computed_at)
        VALUES (?, ?, ?, ?, 'test-model', '2026-07-31T00:00:00Z')
        """,
        (entity_id, encode_f32(vector), source_sample, source_sample),
    )


def _seed_headroom(conn, count: int) -> None:
    for i in range(count):
        conn.execute(
            """
            INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,
                                      weight, evidence_count, valid_from)
            VALUES (?, ?, ?, 'discusses', 1.0, 1, '2026-07-01T00:00:00Z')
            """,
            (f"edg-filler-{i}", f"filler-a-{i}", f"filler-b-{i}"),
        )


def test_status_defaults_and_verdict_empty(conn) -> None:
    status = get_affinity_status(conn)
    assert status["percentile"] == pytest.approx(99.5)
    assert status["active_edges"] == 0
    assert status["verdict"] == "empty"
    assert status["last_recompute"] is None


def test_set_and_nudge_percentile(conn) -> None:
    assert set_affinity_percentile(conn, 99.5) == pytest.approx(99.5)
    fewer = nudge_percentile(conn, "fewer")
    assert fewer["percentile"] == pytest.approx(99.0)
    assert fewer["recompute_needed"] is True
    more = nudge_percentile(conn, "more")
    assert more["percentile"] == pytest.approx(99.5)
    ok = nudge_percentile(conn, "ok")
    assert ok["changed"] is False


def test_apply_config_nudge_returns_status(conn) -> None:
    result = apply_affinity_config(conn, nudge="fewer")
    assert result["percentile"] == pytest.approx(99.0)
    assert result["config_change"]["nudge"] == "fewer"


def test_label_persists_and_hides_from_review_queue(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    _add_centroid(conn, "a", _unit({0: 1.0}))
    _add_centroid(conn, "b", _tilted(1, 0.9))
    _seed_headroom(conn, 40)
    conn.commit()
    rebuild_affinity_edges(conn, percentile=0.0)

    before = list_affinity_pairs_for_review(conn)
    assert any(i["kind"] == "active" for i in before["items"])

    label_affinity_pair(conn, entity_a="b", entity_b="a", label="useful")
    after = list_affinity_pairs_for_review(conn)
    active = [i for i in after["items"] if i["kind"] == "active"]
    assert active == []


def test_co_occurrence_near_miss_has_reason(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    _add_centroid(conn, "a", _unit({0: 1.0}))
    _add_centroid(conn, "b", _tilted(1, 0.9))
    conn.execute(
        """
        INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,
                                  weight, evidence_count, valid_from)
        VALUES ('edg-co', 'a', 'b', ?, 2.0, 2, '2026-07-01T00:00:00Z')
        """,
        (EDGE_CO_OCCURRENCE,),
    )
    _seed_headroom(conn, 40)
    conn.commit()
    rebuild_affinity_edges(conn, percentile=0.0)

    pairs = list_affinity_pairs_for_review(conn)
    co = [i for i in pairs["items"] if i.get("suppress_reason") == "co_occurrence"]
    assert len(co) >= 1
    assert {co[0]["src_entity_id"], co[0]["dst_entity_id"]} == {"a", "b"}


def test_recompute_writes_log_and_status(conn) -> None:
    _add_person(conn, "a", "Ana")
    _add_person(conn, "b", "Bo")
    # Centroids only — recompute_affinity_now rebuilds from mentions; with no
    # mentions the centroid table is wiped. Seed via direct rebuild path after.
    _add_centroid(conn, "a", _unit({0: 1.0}))
    _add_centroid(conn, "b", _tilted(1, 0.9))
    _seed_headroom(conn, 40)
    conn.commit()

    # Direct affinity rebuild (centroids already present).
    affinity = rebuild_affinity_edges(conn, percentile=0.0)
    assert affinity["edges_written"] >= 1
    status = get_affinity_status(conn)
    assert status["last_recompute"] is not None
    assert status["active_edges"] >= 1
    assert status["verdict"] in ("sparse", "balanced", "noisy")


def test_label_rejects_bad_value(conn) -> None:
    with pytest.raises(ValueError):
        label_affinity_pair(conn, entity_a="a", entity_b="b", label="maybe")
