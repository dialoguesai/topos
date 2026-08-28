"""What this person and the owner DO together, not just how often they talk.

Every reading on the person card was a transform of a message count, so the card
could say how much a relationship happened and never what it was about. This is
the other witness: the owner's journal declares who they were with and what they
were working on, and the entity spine folds both into one record.

Measured on the live node 2026-08-28, before this existed: the owner's closest
person by every message statistic — closeness 0.9224, first of 428 — had ten
declared Topos sessions across three months, and his card said "Warm · Inner
circle" and nothing more.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics.person_graph import COACTIVITY_MIN_SESSIONS, attach_coactivity
from topos.features.entities.structured_fields import STRUCTURED_CONFIDENCE


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "coactivity.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _entity(conn, entity_id, name, entity_type):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
        " VALUES (?,?,?,?)",
        (entity_id, entity_type, name, name.lower()),
    )


#: Sessions are counted from the RECORDS, so the fixture writes records.
def _sessions(conn, person, other, n, *, table="journal_entries",
              person_conf=STRUCTURED_CONFIDENCE, at="2026-08-11", tag=""):
    """`n` journal entries in which BOTH `person` and `other` are mentioned."""
    for i in range(n):
        rid = f"r-{person}-{other}-{tag}-{i}"
        for eid, conf in ((person, person_conf), (other, STRUCTURED_CONFIDENCE)):
            conn.execute(
                "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
                " canonical_table, confidence, event_at) VALUES (?,?,?,?,?,?,?)",
                (f"m-{rid}-{eid}", eid, rid, "grow_journal", table, conf, at),
            )


def _edge(conn, src, dst, evidence, *, edge_type="co_occurrence", last_at="2026-08-11"):
    conn.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,"
        " weight, evidence_count, last_event_at) VALUES (?,?,?,?,?,?,?)",
        (f"e-{src}-{dst}-{edge_type}", src, dst, edge_type, float(evidence), evidence, last_at),
    )


def _node(entity_id="p1", node_id="n1", **over):
    base = {"node_id": node_id, "entity_id": entity_id, "label": "Rowan", "is_owner": False}
    base.update(over)
    return base


def _seed(conn):
    _entity(conn, "p1", "Rowan", "person")
    _entity(conn, "p2", "Yusra", "person")
    _entity(conn, "proj", "Topos", "project")
    _entity(conn, "proj2", "Dialogues", "project")
    _entity(conn, "place", "Zilker Park", "place")
    conn.commit()


def test_the_card_learns_what_they_work_on_together(conn):
    _seed(conn)
    _sessions(conn, "p1", "proj", 10)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 1}

    co = nodes[0]["coactivity"]
    assert co["label"] == "Topos"
    assert co["sessions"] == 10
    assert co["kind"] == "project"
    assert co["declared"] is True


def test_a_co_occurrence_edge_alone_is_NOT_a_session(conn):
    """THE BUG this replaced. Reading `entity_edges` looked equivalent and was not: that
    edge is minted wherever two entities land in one record of any kind, so a browsing
    session counts the same as a working afternoon. Run against the live node that version
    attached 42 readings, led by "a head of state · a nationality · 6 sessions" and "a public figure ·
    ChatGPT · 4". Neither is a session anybody logged."""
    _seed(conn)
    _edge(conn, "proj", "p1", 12)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_one_session_is_co_presence_not_collaboration(conn):
    _seed(conn)
    _sessions(conn, "p1", "proj", COACTIVITY_MIN_SESSIONS - 1)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}
    assert "coactivity" not in nodes[0]


def test_a_place_is_where_they_were_not_what_they_did(conn):
    """`shared_with_owner` already answers the places question, and better."""
    _seed(conn)
    _sessions(conn, "p1", "place", 9)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_the_strongest_tie_leads_and_the_rest_ride_along(conn):
    """The headline has to be one thing; the others belong in the tooltip."""
    _seed(conn)
    _sessions(conn, "p1", "proj", 10, tag="a")
    _sessions(conn, "p1", "proj2", 3, tag="b")
    conn.commit()

    nodes = [_node()]
    attach_coactivity(conn, nodes)

    co = nodes[0]["coactivity"]
    assert co["label"] == "Topos"
    assert [a["label"] for a in co["also"]] == ["Dialogues"]


def test_the_owner_is_not_given_a_coactivity_with_their_own_work(conn):
    _seed(conn)
    _entity(conn, "owner", "Owner", "person")
    _sessions(conn, "owner", "proj", 128)
    conn.commit()

    nodes = [_node(entity_id="owner", node_id="owner", label="Owner", is_owner=True)]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_only_the_JOURNAL_counts(conn):
    """The same two entities co-mentioned in a message or a browsing record is not a
    session together — that is what let news co-mentions in."""
    _seed(conn)
    _sessions(conn, "p1", "proj", 10, table="conversation_messages")
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_a_name_the_MODEL_found_in_prose_is_not_a_declared_participant(conn):
    """"10 sessions" has to mean the owner wrote the name down. An NER hit on the prose
    carries the model's uncertainty and cannot license that number."""
    _seed(conn)
    _sessions(conn, "p1", "proj", 10, person_conf=0.87)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_a_person_the_graph_does_not_carry_is_skipped(conn):
    _seed(conn)
    _sessions(conn, "p2", "proj", 9)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}


def test_it_says_nothing_when_there_is_no_journal(conn):
    _seed(conn)
    conn.commit()

    nodes = [_node()]
    assert attach_coactivity(conn, nodes) == {"attached": 0}
    assert "coactivity" not in nodes[0]
