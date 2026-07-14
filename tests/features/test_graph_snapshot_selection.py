"""selection=weight vs selection=all + pagination meta for owner Load more."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import graph_snapshot, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_starvation(conn):
    """Heavy old co_occurrence + lighter recent pursues — weight slice drops goals."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    other = r._create_entity("Other", "person")
    goal = r._create_entity("Ship the graph", "goal")
    conn.commit()

    update_edge(
        conn,
        src_entity_id=owner,
        dst_entity_id=other,
        edge_type="co_occurrence",
        event_at="2025-01-01T00:00:00Z",
        increment=20.0,
    )
    update_edge(
        conn,
        src_entity_id=owner,
        dst_entity_id=goal,
        edge_type="pursues",
        event_at="2026-07-10T00:00:00Z",
        increment=2.0,
    )
    conn.execute(
        "UPDATE entity_edges SET last_event_at='2025-01-01T00:00:00Z', "
        "valid_from='2025-01-01T00:00:00Z', weight=20.0 "
        "WHERE edge_type='co_occurrence'"
    )
    conn.execute(
        "UPDATE entity_edges SET last_event_at='2026-07-10T00:00:00Z', "
        "valid_from='2026-07-10T00:00:00Z', weight=2.0 "
        "WHERE edge_type='pursues'"
    )
    conn.commit()
    return owner, other, goal


def test_weight_selection_prefers_heavy_edges(conn):
    owner, other, goal = _seed_starvation(conn)
    snap = graph_snapshot(conn, selection="weight", limit_edges=1, limit_nodes=10)
    assert snap["meta"]["selection"] == "weight"
    assert len(snap["edges"]) == 1
    assert snap["edges"][0]["edge_type"] == "co_occurrence"
    types = {e["edge_type"] for e in snap["edges"]}
    assert "pursues" not in types
    assert snap["meta"]["truncated_edges"] is True
    assert snap["meta"]["total_edges_matching"] == 2
    assert snap["meta"]["next_offset"] == 1


def test_all_selection_prefers_recent_pursues(conn):
    owner, other, goal = _seed_starvation(conn)
    snap = graph_snapshot(conn, selection="all", limit_edges=1, limit_nodes=10)
    assert snap["meta"]["selection"] == "all"
    assert len(snap["edges"]) == 1
    assert snap["edges"][0]["edge_type"] == "pursues"
    assert snap["edges"][0]["dst_node_id"] == goal or snap["edges"][0]["src_node_id"] == goal


def test_pagination_second_page_disjoint(conn):
    owner, other, goal = _seed_starvation(conn)
    page1 = graph_snapshot(conn, selection="all", limit_edges=1, limit_nodes=10, offset=0)
    assert page1["meta"]["next_offset"] == 1
    page2 = graph_snapshot(
        conn,
        selection="all",
        limit_edges=1,
        limit_nodes=10,
        offset=page1["meta"]["next_offset"],
    )
    ids1 = {e["edge_id"] for e in page1["edges"]}
    ids2 = {e["edge_id"] for e in page2["edges"]}
    assert ids1.isdisjoint(ids2)
    assert len(ids1 | ids2) == 2
    assert page2["meta"]["truncated_edges"] is False
    assert page2["meta"]["next_offset"] is None


def test_default_selection_is_weight_with_meta(conn):
    _seed_starvation(conn)
    snap = graph_snapshot(conn)
    assert "meta" in snap
    assert snap["meta"]["selection"] == "weight"
    assert snap["meta"]["offset"] == 0
    assert snap["meta"]["returned_edges"] == len(snap["edges"])
    assert snap["meta"]["returned_nodes"] == len(snap["nodes"])
