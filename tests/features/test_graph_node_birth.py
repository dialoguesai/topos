"""Node birth dates in the graph payload must cover mention-less hub nodes.

`graph_snapshot` derived `first_event_at` purely from `entity_mentions`, which
is a sighting log. Goal / topic / conversation hubs are minted as VERTICES, not
sightings, so they have no mention row and arrived at the timeline undatable --
on a live node, 60 of 100 returned nodes carried no birth date while the
`entities.first_seen` column was 100% populated. The enrichers write that span
from the source records' event times; nothing read it back.
"""

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


def _nodes_by_id(conn, **kw):
    snap = graph_snapshot(conn, **kw)
    return {n["node_id"]: n for n in snap["nodes"]}


def test_hub_node_uses_stored_birth_when_it_has_no_mentions(conn):
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type,"
        " mention_count, first_seen, last_seen) VALUES (?,?,?,?,0,?,?)",
        ("goal_abc", "Ship the thing", "ship the thing", "goal",
         "2026-02-01T00:00:00Z", "2026-05-01T00:00:00Z"),
    )
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id="goal_abc",
                edge_type="pursues", event_at="2026-02-01T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    # The hub has zero mention rows, so only the stored span can date it.
    assert nodes["goal_abc"]["first_event_at"] == "2026-02-01T00:00:00Z"


def test_mention_birth_wins_over_stored_span(conn):
    """A real sighting is finer-grained than the enricher's stored span."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    person = r._create_entity("Ada", "person")
    conn.execute(
        "UPDATE entities SET first_seen=? WHERE entity_id=?",
        ("2026-09-09T00:00:00Z", person),
    )
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, record_id, event_at, confidence)"
        " VALUES (?,?,?,?)",
        (person, "rec1", "2026-01-15T00:00:00Z", 0.9),
    )
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id=person,
                edge_type="communicates_with", event_at="2026-01-15T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    assert nodes[person]["first_event_at"] == "2026-01-15T00:00:00Z"


def test_epoch_junk_in_stored_span_is_not_a_birth_date(conn):
    """A 1970 date would drag the node to the far left of every timeline."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type,"
        " mention_count, first_seen) VALUES (?,?,?,?,0,?)",
        ("goal_junk", "Undated goal", "undated goal", "goal", "1970-01-01T00:00:00Z"),
    )
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id="goal_junk",
                edge_type="pursues", event_at="2026-02-01T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    assert nodes["goal_junk"]["first_event_at"] is None


def test_no_node_type_is_systematically_undatable(conn):
    """The shape of the original bug: an entire node TYPE dated at 0%.

    A per-node assertion would not have caught it -- person nodes were fine.
    """
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    for i, (eid, etype) in enumerate(
        [("goal_1", "goal"), ("topic_1", "topic"), ("conv_1", "conversation")]
    ):
        conn.execute(
            "INSERT INTO entities (entity_id, canonical_name, normalized_name,"
            " entity_type, mention_count, first_seen) VALUES (?,?,?,?,0,?)",
            (eid, f"Hub {i}", f"hub {i}", etype, "2026-03-01T00:00:00Z"),
        )
        conn.commit()
        update_edge(conn, src_entity_id=owner, dst_entity_id=eid,
                    edge_type="pursues", event_at="2026-03-01T00:00:00Z")
    conn.commit()

    nodes = graph_snapshot(conn)["nodes"]
    dated_by_type: dict[str, list[bool]] = {}
    for n in nodes:
        dated_by_type.setdefault(n["node_type"], []).append(bool(n["first_event_at"]))
    undatable = [t for t, flags in dated_by_type.items() if not any(flags)]
    assert not undatable, f"node types with no birth date at all: {undatable}"
