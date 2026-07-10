"""Goals / places / conversations materialized into the entity graph.

Each enricher is additive, mz-tagged (refreshed with the fact materializer's
full-refresh cycle) and skips cleanly when its feed table is absent.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.edges import graph_snapshot
from topos.features.entities.graph_enrichers import materialize_graph_enrichments
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _owner(conn) -> str:
    r = EntityResolver(conn)
    eid = r._create_entity("Owner", "person")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (eid,))
    conn.commit()
    return eid


def _edges(conn, edge_type):
    snap = graph_snapshot(conn, min_weight=0.0)
    return [e for e in snap["edges"] if e["edge_type"] == edge_type]


def _labels(conn):
    return {n["label"]: n for n in graph_snapshot(conn, min_weight=0.0)["nodes"]}


# ------------------------------------------------------------------- goals


def test_goal_becomes_node_linked_to_owner_and_record_entities(conn):
    owner = _owner(conn)
    r = EntityResolver(conn)
    marcus = r._create_entity("Marcus", "person")
    conn.commit()
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
        "VALUES ('g1', 'msg-9', 'chatgpt_file_ingestion', 'Get an intro to Marcus', '{}')"
    )
    # the goal's provenance record also mentions Marcus → goal relates to him
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES ('m1', ?, 'msg-9', 'chatgpt_file_ingestion', 'Marcus', 0.9, '2026-06-01')",
        (marcus,),
    )
    conn.commit()

    out = materialize_graph_enrichments(conn)
    assert out["goal_edges"] >= 2  # owner→goal + goal→Marcus

    pursues = _edges(conn, "pursues")
    assert len(pursues) == 1
    assert pursues[0]["src_node_id"] == owner
    meta = json.loads(pursues[0]["metadata_json"] or "{}")
    assert meta.get("mz") == 1
    assert meta.get("actor_role") == "authored"

    labels = _labels(conn)
    assert "Get an intro to Marcus" in labels
    assert labels["Get an intro to Marcus"]["node_type"] == "goal"
    assert len(_edges(conn, "relates_to")) == 1


# ------------------------------------------------------------------ places


def test_visits_become_located_at_edges_weighted_by_count(conn):
    owner = _owner(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
            f"VALUES ('l{i}', 'LA Fitness', '2026-06-0{i + 1}T10:00:00Z', 'grow_journal')"
        )
    conn.commit()

    out = materialize_graph_enrichments(conn)
    assert out["place_edges"] == 1

    located = _edges(conn, "located_at")
    assert len(located) == 1
    assert located[0]["src_node_id"] == owner
    assert located[0]["last_event_at"] == "2026-06-03T10:00:00Z"
    meta = json.loads(located[0]["metadata_json"] or "{}")
    assert meta.get("visit_count") == 3
    labels = _labels(conn)
    assert labels["LA Fitness"]["node_type"] == "place"


# ----------------------------------------------------------- conversations


def _seed_conversation(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversations ("
        "conversation_id TEXT PRIMARY KEY, dataset_id TEXT, source_id TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_messages ("
        "message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_type TEXT, "
        "sender_id TEXT, event_at TEXT, actor_role TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_participants ("
        "conversation_id TEXT, dataset_id TEXT, source_id TEXT, contact_id TEXT, role TEXT)"
    )
    conn.execute(
        "INSERT INTO conversations (conversation_id, source_id) VALUES ('conv1', 'voxterm_transcripts')"
    )
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, event_at) "
        "VALUES ('cm1', 'conv1', '2026-07-01T00:00:00Z')"
    )
    conn.commit()


def test_conversation_node_links_mentions_and_participants(conn):
    _owner(conn)
    r = EntityResolver(conn)
    topic = r._create_entity("Provenance", "topic")
    conn.commit()
    _seed_conversation(conn)
    # entity mentioned inside the conversation
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES ('m2', ?, 'cm1', 'voxterm_transcripts', 'Provenance', 0.9, '2026-07-01')",
        (topic,),
    )
    # a participant anchored to a contact with an entity
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
        "VALUES ('c-nick', 'ds', 'src', 'Nick', 0)"
    )
    nick = r._create_entity("Nick", "person")
    conn.execute("UPDATE entities SET contact_id='c-nick' WHERE entity_id=?", (nick,))
    conn.execute(
        "INSERT INTO conversation_participants (conversation_id, contact_id) VALUES ('conv1', 'c-nick')"
    )
    conn.commit()

    out = materialize_graph_enrichments(conn)
    assert out["conversation_edges"] >= 2  # mentions + participates_in

    mentions = _edges(conn, "mentions")
    participates = _edges(conn, "participates_in")
    assert len(mentions) == 1 and len(participates) == 1
    labels = _labels(conn)
    conv_nodes = [n for n in labels.values() if n["node_type"] == "conversation"]
    assert len(conv_nodes) == 1


def test_enrichers_skip_missing_tables_and_are_idempotent(conn):
    _owner(conn)
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
        "VALUES ('l1', 'Zilker', '2026-06-01T10:00:00Z', 'grow_journal')"
    )
    conn.commit()
    first = materialize_graph_enrichments(conn)  # conversations tables absent → skipped
    n1 = conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    second = materialize_graph_enrichments(conn)
    n2 = conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    assert first == second
    assert n1 == n2
    assert first["conversation_edges"] == 0
