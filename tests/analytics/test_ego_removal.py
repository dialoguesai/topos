"""F5 — the owner is removed before centrality and community detection.

The owner sits inside every conversation they are part of, so co-participation
makes them adjacent to nearly everyone. Measured live 2026-08-26, the owner was
the most central node in every period (degree 0.375–0.582, betweenness
0.146–0.329), and the partition collapsed around them:

    period    communities        largest community
    2026-04   34 ->  53          35% -> 15%
    2026-05   40 ->  63          34% ->  6%
    2026-06   34 ->  54          37% -> 17%
    2026-07   31 ->  48          34% -> 18%
    2026-08   12 ->  37          58% -> 20%

August's "community" of 58% of all participants was the owner's star, not a group
of people who know one another.

The split that matters: the ego is removed from the GRAPH, never from the stored
EDGES. Edges record who talked to whom and the owner's are the most important ones
there; centrality and communities describe structure BETWEEN other people. Only the
second question needs the ego gone.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics.messenger_communities import (
    MESSENGER_SOCIAL_EDGES_TABLE,
    _owner_participant_ids,
    build_networkx_graph,
    compute_importance_and_communities,
    ensure_messenger_analytics_tables,
)


def _star(centre: str, spokes: int) -> dict:
    """The shape the owner makes: everyone adjacent to one node, nobody else joined."""
    nodes = [{"id": centre}] + [{"id": f"p{i}"} for i in range(spokes)]
    edges = [{"source": centre, "target": f"p{i}", "weight": 1.0} for i in range(spokes)]
    return {"nodes": nodes, "edges": edges}


def test_the_ego_is_gone_from_the_graph():
    g = build_networkx_graph(_star("owner", 5), exclude={"owner"})
    assert "owner" not in g.nodes()
    assert g.number_of_nodes() == 5


def test_edges_touching_the_ego_go_with_it():
    """A dangling edge to a removed node would re-add it via add_edge."""
    g = build_networkx_graph(_star("owner", 5), exclude={"owner"})
    assert g.number_of_edges() == 0
    assert "owner" not in g.nodes()


def test_the_star_collapses_the_partition_until_the_ego_is_removed():
    """The regression, in miniature: one node making one fake community."""
    payload = _star("owner", 6)
    with_ego = compute_importance_and_communities(payload)
    without = compute_importance_and_communities(payload, exclude={"owner"})

    # With the ego, everyone is transitively connected — one community.
    assert len(set(with_ego["communities"].values())) == 1
    # Without it, the spokes share no edge: six separate people, not one group.
    assert len(set(without["communities"].values())) == 6
    assert "owner" not in without["communities"]
    assert "owner" not in without["importance"]


def test_a_real_group_survives_ego_removal():
    """Ego removal must not dissolve groups that genuinely know each other."""
    payload = {
        "nodes": [{"id": x} for x in ("owner", "a", "b", "c")],
        "edges": [
            {"source": "owner", "target": "a", "weight": 1.0},
            {"source": "owner", "target": "b", "weight": 1.0},
            {"source": "owner", "target": "c", "weight": 1.0},
            {"source": "a", "target": "b", "weight": 5.0},
            {"source": "b", "target": "c", "weight": 5.0},
        ],
    }
    out = compute_importance_and_communities(payload, exclude={"owner"})
    comms = out["communities"]
    assert {"a", "b", "c"} <= set(comms)
    assert comms["a"] == comms["b"] == comms["c"], "a genuinely connected trio stays one community"


def test_no_exclusion_is_the_old_behaviour():
    """Absent an owner, nothing changes — the parameter is opt-in."""
    payload = _star("owner", 4)
    assert compute_importance_and_communities(payload)["communities"] == \
        compute_importance_and_communities(payload, exclude=set())["communities"]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "e.db"))
    c.execute(
        "CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, is_self INTEGER DEFAULT 0)"
    )
    ensure_messenger_analytics_tables(c)
    yield c
    c.close()


def test_owner_resolution_returns_every_self_contact(conn):
    """Plural on purpose — this machine has two, and missing one leaves the ego in
    the graph under its other identity, which looks like the fix working."""
    conn.executemany(
        "INSERT INTO contacts (contact_id, is_self) VALUES (?, ?)",
        [("ds:contact:me", 1), ("test-dataset:contact:me", 1), ("ds:contact:someone", 0)],
    )
    conn.commit()
    assert _owner_participant_ids(conn) == {"ds:contact:me", "test-dataset:contact:me"}


def test_owner_resolution_is_safe_without_a_contacts_table(tmp_path):
    """Analytics must not break on a database that predates contacts."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    assert _owner_participant_ids(c) == set()
    c.close()


# --- the entity-graph half: fingerprints ---

def test_fingerprint_excludes_the_ego_and_reclaims_the_slot():
    """The ego both consumes a core slot and skews the weight.

    Measured live: 1 of 127 fingerprints held the ego, but at 0.598 of the total
    weight across two slots — that community's recorded identity was mostly the owner.
    """
    from topos.features.entities.community_names import core_fingerprint

    ranked = ["owner", "a", "b", "c"]
    weights = {"owner": 10.0, "a": 3.0, "b": 2.0, "c": 1.0}

    with_ego = core_fingerprint(ranked, weights, k=3)
    without = core_fingerprint(ranked, weights, k=3, exclude={"owner"})

    assert [e for e, _ in with_ego] == ["owner", "a", "b"]
    # c is pulled in: excluding the ego reclaims the slot rather than shortening the core
    assert [e for e, _ in without] == ["a", "b", "c"]
    assert dict(with_ego)["owner"] > 0.6, "the ego dominated the weight"
    assert abs(sum(w for _, w in without) - 1.0) < 1e-9, "weights still normalize"


def test_ego_inflates_similarity_between_unrelated_communities():
    """Why it biases MATCHING, not just aesthetics.

    Two communities sharing only the owner should not look alike. With the ego in
    the core they cross the 0.5 match threshold; without it they do not.
    """
    from topos.features.entities.community_names import core_fingerprint, weighted_jaccard

    weights = {"owner": 10.0, "a": 1.0, "b": 1.0, "x": 1.0, "y": 1.0}
    one = ["owner", "a", "b"]
    two = ["owner", "x", "y"]

    sim_with = weighted_jaccard(core_fingerprint(one, weights), core_fingerprint(two, weights))
    sim_without = weighted_jaccard(
        core_fingerprint(one, weights, exclude={"owner"}),
        core_fingerprint(two, weights, exclude={"owner"}),
    )
    assert sim_with > 0.5, "the shared ego alone pushed them over the match threshold"
    assert sim_without == 0.0, "with no shared members they are correctly unrelated"
