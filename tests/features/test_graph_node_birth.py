"""Node birth dates in the graph payload must come from EVIDENCE.

`graph_snapshot` derived `first_event_at` purely from `entity_mentions`, which
is a sighting log. Goal / topic / conversation hubs are minted as VERTICES, not
sightings, so they have no mention row and arrived at the timeline undatable --
on a live node, 60 of 100 returned nodes carried no birth date.

`entities.first_seen` is the obvious fallback and the wrong one: `_create_entity`
stamps it with the mint clock and only entities that later receive a mention get
it recomputed from evidence. On the owner's node, two address-book import batches
left 1,190 of 1,599 people sharing exactly two timestamps, so dating from that
column put everyone in the address book on the day their contacts were read --
which is how people last spoken to in May turned up in the most recent few days.

Edges carry real event stamps, and every node that reaches the payload has one.
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
    return {n["node_id"]: n for n in graph_snapshot(conn, **kw)["nodes"]}


def test_hub_node_is_dated_by_its_edges_when_it_has_no_mentions(conn):
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type,"
        " mention_count) VALUES (?,?,?,?,0)",
        ("goal_abc", "Ship the thing", "ship the thing", "goal"),
    )
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id="goal_abc",
                edge_type="pursues", event_at="2026-02-01T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    assert nodes["goal_abc"]["first_event_at"] == "2026-02-01T00:00:00Z"


def test_the_import_clock_never_becomes_a_birth_date(conn):
    """The live defect, reproduced.

    A contact-seeded person carries a mint-time `first_seen` shared with every
    other row in the same import batch. Their real activity is months earlier;
    the payload must report the activity, not the import.
    """
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    people = [r._create_entity(f"Person {i}", "person") for i in range(3)]
    # One import batch: identical stamp, zero mentions -- the shape on the node.
    conn.execute(
        "UPDATE entities SET first_seen=?, last_seen=?, mention_count=0"
        " WHERE entity_id IN (?,?,?)",
        ("2026-08-26T02:40:05Z", "2026-08-26T02:40:05Z", *people),
    )
    conn.commit()
    for i, p in enumerate(people):
        update_edge(conn, src_entity_id=owner, dst_entity_id=p,
                    edge_type="communicates_with",
                    event_at=f"2026-0{i + 4}-15T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    births = {nodes[p]["first_event_at"] for p in people}
    assert "2026-08-26T02:40:05Z" not in births, "the import stamp reached the payload"
    assert len(births) == 3, f"one batch stamp collapsed three people onto it: {births}"


def test_the_earlier_of_the_two_edge_stamps_wins(conn):
    """`valid_from` is belief time, `last_event_at` is event time.

    An ingest stamp is always later than the event it records, so the earlier of
    the pair is the one grounded in the world -- no per-edge-type special case.
    """
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    person = r._create_entity("Ada", "person")
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id=person,
                edge_type="communicates_with", event_at="2026-01-15T00:00:00Z")
    # Ingest ran months after the conversation happened.
    conn.execute(
        "UPDATE entity_edges SET valid_from='2026-08-26T02:42:39Z',"
        " last_event_at='2026-01-15T00:00:00Z' WHERE dst_entity_id=?", (person,))
    conn.commit()

    nodes = _nodes_by_id(conn)
    assert nodes[person]["first_event_at"] == "2026-01-15T00:00:00Z"


def test_mention_birth_wins_over_edge_evidence(conn):
    """A real sighting is finer-grained than an edge rollup."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    person = r._create_entity("Ada", "person")
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, record_id, event_at, confidence)"
        " VALUES (?,?,?,?)", (person, "rec1", "2026-01-15T00:00:00Z", 0.9))
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id=person,
                edge_type="communicates_with", event_at="2026-03-20T00:00:00Z")
    conn.commit()

    nodes = _nodes_by_id(conn)
    assert nodes[person]["first_event_at"] == "2026-01-15T00:00:00Z"


def test_epoch_junk_is_not_a_birth_date(conn):
    """A 1970 date would drag the node to the far left of every timeline."""
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type,"
        " mention_count) VALUES (?,?,?,?,0)",
        ("goal_junk", "Undated goal", "undated goal", "goal"),
    )
    conn.commit()
    update_edge(conn, src_entity_id=owner, dst_entity_id="goal_junk",
                edge_type="pursues", event_at="2026-02-01T00:00:00Z")
    conn.execute("UPDATE entity_edges SET valid_from='1970-01-01T00:00:00Z',"
                 " last_event_at='1970-01-01T00:00:00Z' WHERE dst_entity_id='goal_junk'")
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
            " entity_type, mention_count) VALUES (?,?,?,?,0)",
            (eid, f"Hub {i}", f"hub {i}", etype),
        )
        conn.commit()
        update_edge(conn, src_entity_id=owner, dst_entity_id=eid,
                    edge_type="pursues", event_at="2026-03-01T00:00:00Z")
    conn.commit()

    dated_by_type: dict[str, list[bool]] = {}
    for n in graph_snapshot(conn)["nodes"]:
        dated_by_type.setdefault(n["node_type"], []).append(bool(n["first_event_at"]))
    undatable = [t for t, flags in dated_by_type.items() if not any(flags)]
    assert not undatable, f"node types with no birth date at all: {undatable}"
