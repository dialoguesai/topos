"""The graph page must not be swallowed by one relation.

``graph_snapshot`` ordered every edge by ``weight DESC`` and took the top N. But
the weights are incommensurable units: ``communicates_with`` accumulates message
counts and reaches 2,772, ``co_occurrence`` accumulates co-mention counts and
tops out near 7, ``located_at`` is a computed 2.25–10 band. Sorting them together
is comparing message volume against co-mention frequency.

Measured on the owner's node 2026-08-27, with the shipped 300-edge page:

  * ``communicates_with`` — **4.9%** of the graph (267 of 5,445 edges) — took
    **203 of 300 slots**;
  * ``relates_to`` — the LARGEST relation at 2,030 edges, **37%** of the graph —
    got **zero**, as did ``discusses`` (178) and ``participates_in`` (177).

Two layers had to change, because fixing only the first left the bias intact one
level down: the edge query allocates per type, and node collection interleaves so
the ``limit_nodes`` cap truncates evenly. Before the second fix, ``relates_to``
recovered 91 slots in the query and collapsed back to 6 in the response, because
node ids were gathered in weight order and the heaviest relation consumed the
whole node budget.

Explicitly NOT fixed here, and that is the decision rather than an oversight: a
single co-occurrence is weight 1.0 and still will not reach a top-N-by-strength
overview. Surfacing it would mean surfacing every weight-1 edge — a different
view, not a better ranking. The entity-scoped detail view already shows it, which
is the path someone actually takes to ask "who was I with there".
"""

from __future__ import annotations

import collections
import sqlite3

import pytest

from topos.features.entities.edges import _allocate_edge_budget, graph_snapshot


# ------------------------------------------------------------ the allocator


def test_a_dominant_type_cannot_take_the_whole_page():
    counts = [("communicates_with", 267), ("relates_to", 2030), ("co_occurrence", 933)]

    budget = _allocate_edge_budget(counts, 300)

    assert sum(budget.values()) <= 300
    assert budget["relates_to"] > budget["communicates_with"], (
        "the largest relation must not lose to the heaviest-weighted one"
    )


def test_a_small_type_still_gets_a_floor():
    """Proportional alone is just a slower version of the same bug.

    ``health.medication`` has one edge on the live node. Without a floor it
    rounds to zero and the relation is invisible forever.
    """
    counts = [("huge", 5000), ("tiny", 1)]

    budget = _allocate_edge_budget(counts, 300)

    assert budget["tiny"] >= 1


def test_no_type_is_allocated_more_than_it_has():
    budget = _allocate_edge_budget([("a", 3), ("b", 5000)], 300)

    assert budget["a"] == 3


def test_leftovers_go_to_the_largest_types():
    """A page should still fill up when small types cannot use their share."""
    budget = _allocate_edge_budget([("big", 5000), ("a", 2), ("b", 2)], 100)

    assert sum(budget.values()) == 100


def test_degenerate_inputs_are_safe():
    assert _allocate_edge_budget([], 300) == {}
    assert _allocate_edge_budget([("a", 5)], 0) == {}
    assert _allocate_edge_budget([("a", 0)], 300) == {}


# ------------------------------------------------------------ end to end


def _seed(conn):
    from topos.storage.db.migrations import apply_all_migrations

    apply_all_migrations(conn)
    # One dominant heavy relation, one large light relation, one tiny relation —
    # the live shape in miniature.
    plan = [("communicates_with", 40, 900.0), ("relates_to", 300, 2.0), ("located_at", 3, 5.0)]
    n = 0
    for edge_type, count, weight in plan:
        for i in range(count):
            a, b = f"ent-{edge_type}-{i}-a", f"ent-{edge_type}-{i}-b"
            for eid in (a, b):
                conn.execute(
                    "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name,"
                    " normalized_name, mention_count, is_self) VALUES (?,?,?,?,1,0)",
                    (eid, "person", eid, eid),
                )
            conn.execute(
                "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,"
                " weight, evidence_count, valid_from) VALUES (?,?,?,?,?,1,?)",
                (f"e{n}", a, b, edge_type, weight, "2026-07-01"),
            )
            n += 1
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "graph.db"))
    _seed(c)
    yield c
    c.close()


def _types(page):
    return collections.Counter(e.get("edge_type") for e in (page.get("edges") or []))


def test_the_page_is_not_one_relation(conn):
    page = graph_snapshot(conn, limit_nodes=2000, limit_edges=200)

    comp = _types(page)
    assert comp["relates_to"] > comp["communicates_with"], (
        f"the heaviest relation still dominates: {dict(comp)}"
    )
    assert len(comp) >= 3, "every relation present in the graph should be represented"


def test_a_light_relation_is_not_starved(conn):
    """``relates_to`` weight 2.0 against ``communicates_with`` weight 900."""
    page = graph_snapshot(conn, limit_nodes=2000, limit_edges=200)

    assert _types(page)["relates_to"] > 0


def test_a_tiny_relation_survives(conn):
    page = graph_snapshot(conn, limit_nodes=2000, limit_edges=200)

    assert _types(page)["located_at"] > 0


def test_the_node_cap_truncates_evenly(conn):
    """The second layer. Fixing only the query left the bias one level down.

    Node ids used to be collected in weight order, so a binding ``limit_nodes``
    handed the whole budget back to the heaviest relation — ``relates_to``
    recovered 91 slots in the query and collapsed to 6 in the response.
    """
    page = graph_snapshot(conn, limit_nodes=60, limit_edges=200)

    comp = _types(page)
    assert len(comp) >= 3, f"a binding node cap collapsed the page to {dict(comp)}"
    assert comp["relates_to"] > 0


def test_ordering_within_a_type_is_still_by_weight(conn):
    """`selection` keeps its meaning — weight just stops crossing units."""
    for eid in ("ent-heavy-a", "ent-heavy-b"):
        conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name,"
            " normalized_name, mention_count, is_self) VALUES (?,?,?,?,1,0)",
            (eid, "person", eid, eid),
        )
    conn.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,"
        " weight, evidence_count, valid_from) VALUES (?,?,?,?,?,1,?)",
        ("e-heavy", "ent-heavy-a", "ent-heavy-b", "relates_to", 99.0, "2026-07-01"),
    )
    conn.commit()

    page = graph_snapshot(conn, limit_nodes=2000, limit_edges=200)
    relates = [e for e in page["edges"] if e.get("edge_type") == "relates_to"]

    assert relates and relates[0].get("weight") == 99.0


def test_the_meta_still_reports_truncation(conn):
    """A page that cannot show everything must say so."""
    page = graph_snapshot(conn, limit_nodes=60, limit_edges=200)

    meta = page.get("meta") or {}
    assert meta.get("total_edges_matching") == 343
    assert meta.get("returned_edges") == len(page["edges"])
    assert meta.get("truncated_edges") is True
