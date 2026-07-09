"""graph_snapshot temporal views: active / include_closed / as_of point-in-time.

Backs the graph-UI temporal scrubber — the graph as it stood at an instant is
the set of edges whose validity window covers that instant.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import graph_snapshot, supersede_edge, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed(conn):
    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Bram", "person")
    d = r._create_entity("Cass", "person")
    conn.commit()
    # A—B relationship held 2025 then ENDED 2026-03; A—Cass started 2026-04.
    update_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type="communicates_with",
                event_at="2025-06-01T00:00:00Z")
    supersede_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type="communicates_with",
                   valid_to="2026-03-01T00:00:00Z")
    update_edge(conn, src_entity_id=a, dst_entity_id=d, edge_type="communicates_with",
                event_at="2026-04-01T00:00:00Z")
    # update_edge stamps valid_from at ingest-now (belief-validity); rewrite to
    # realistic historical starts so the point-in-time windows are coherent
    # (A-B held 2025-06..2026-03; A-Cass active since 2026-04).
    conn.execute("UPDATE entity_edges SET valid_from='2025-06-01T00:00:00Z' WHERE valid_to='2026-03-01T00:00:00Z'")
    conn.execute("UPDATE entity_edges SET valid_from='2026-04-01T00:00:00Z' WHERE valid_to IS NULL AND edge_type='communicates_with'")
    conn.commit()
    return a, b, d


def _edge_pairs(snap):
    return {(e["src_node_id"], e["dst_node_id"]) for e in snap["edges"]}


def test_active_view_hides_ended_edge(conn):
    a, b, d = _seed(conn)
    snap = graph_snapshot(conn)  # active only
    pairs = _edge_pairs(snap)
    assert (a, d) in pairs or (d, a) in pairs  # current edge present
    assert (a, b) not in pairs and (b, a) not in pairs  # ended edge hidden
    # active edges carry valid_to=None; the field rides first-class for the UI
    assert all(e["valid_to"] is None for e in snap["edges"])


def test_include_closed_adds_history(conn):
    a, b, d = _seed(conn)
    snap = graph_snapshot(conn, include_closed=True)
    pairs = _edge_pairs(snap)
    assert ({(a, b), (b, a)} & pairs)  # the ended A-B edge is back as history
    closed = [e for e in snap["edges"] if e["valid_to"] is not None]
    assert closed and all(e["valid_to"] for e in closed)


def test_as_of_returns_point_in_time_graph(conn):
    a, b, d = _seed(conn)
    # In 2025-12 the A-B edge was active and A-Cass did not yet exist.
    past = graph_snapshot(conn, as_of="2025-12-01T00:00:00Z")
    ppairs = _edge_pairs(past)
    assert ({(a, b), (b, a)} & ppairs)          # A-B held then
    assert (a, d) not in ppairs and (d, a) not in ppairs  # A-Cass not yet started
    # After 2026-04 the reverse holds
    now = graph_snapshot(conn, as_of="2026-05-01T00:00:00Z")
    npairs = _edge_pairs(now)
    assert ({(a, d), (d, a)} & npairs)
    assert (a, b) not in npairs and (b, a) not in npairs
