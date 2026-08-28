"""Goals / places / conversations materialized into the entity graph.

Each enricher is additive, mz-tagged (upserted in place; stale edges are
removed by the rebuild's end-of-run sweep) and skips cleanly when its feed
table is absent.
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


def test_goal_edges_carry_source_event_time_not_extraction_time(conn):
    """The temporal graph must date goals by WHEN THEY HAPPENED (the source
    record's event time via timeline), not when extraction ran — re-extraction
    stamped every goal 'today' and collapsed weeks of goals into the scrubber's
    last 2 days."""
    _owner(conn)
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json, created_at) "
        "VALUES ('g2', 'msg-7', 'chatgpt_file_ingestion', 'Ship the pilot', '{}', '2026-07-10T00:00:00Z')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table) "
        "VALUES ('2025-03-14T09:00:00Z', 'msg-7', 'chatgpt_file_ingestion', 'ai_chat_messages')"
    )
    conn.commit()

    materialize_graph_enrichments(conn)
    pursues = _edges(conn, "pursues")
    assert len(pursues) == 1
    assert str(pursues[0]["valid_from"]).startswith("2025-03-14")
    assert str(pursues[0]["last_event_at"]).startswith("2025-03-14")


def test_similar_goal_texts_cluster_into_one_node(conn):
    """Near-duplicate natural-language goals (never string-identical) cluster
    by embedding cosine; the surviving node lists every bundled variant and
    its weight reflects the combined occurrences."""
    owner = _owner(conn)
    texts = [
        "Deepen Orion scope coverage",
        "Deepen the Orion scope coverage work this week",
        "Plan a trip to Yosemite",  # unrelated — must stay its own node
    ]
    for i, t in enumerate(texts):
        conn.execute(
            "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
            "VALUES (?, ?, 'chatgpt_file_ingestion', ?, '{}')",
            (f"sg{i}", f"sr{i}", t),
        )
        conn.execute(
            "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table) "
            f"VALUES ('2026-06-0{i + 1}T09:00:00Z', 'sr{i}', 'chatgpt_file_ingestion', 'ai_chat_messages')"
        )
    conn.commit()

    # Injectable embedder: first two texts share a vector; the third is orthogonal.
    def fake_embed(batch):
        return [[1.0, 0.0] if "Orion" in t else [0.0, 1.0] for t in batch]

    import json as _json

    materialize_graph_enrichments(conn, goal_embed_fn=fake_embed)
    labels = _labels(conn)
    goal_nodes = [n for n in labels.values() if n["node_type"] == "goal"]
    assert len(goal_nodes) == 2  # Orion pair clustered; Yosemite separate

    orion = next(n for n in goal_nodes if "Orion" in str(n["label"]))
    meta = _json.loads(orion.get("metadata_json") or "{}")
    variants = meta.get("goal_variants") or []
    assert len(variants) == 2 and any("this week" in v for v in variants)

    pursues = _edges(conn, "pursues")
    orion_edge = next(e for e in pursues if e["dst_node_id"] == orion["node_id"])
    assert orion_edge["weight"] > _edges(conn, "pursues")[0]["weight"] * 0 + 2.0  # bumped past floor
    assert str(orion_edge["valid_from"]).startswith("2026-06-01")
    assert str(orion_edge["last_event_at"]).startswith("2026-06-02")


def test_goal_clustering_falls_back_to_token_similarity(conn):
    """No embedder available → high token-overlap still clusters."""
    _owner(conn)
    for i, t in enumerate(["review the topos one pager", "review topos one pager again"]):
        conn.execute(
            "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
            "VALUES (?, ?, 'src', ?, '{}')",
            (f"tk{i}", f"tr{i}", t),
        )
    conn.commit()
    materialize_graph_enrichments(conn, goal_embed_fn=lambda batch: None)
    labels = _labels(conn)
    goal_nodes = [n for n in labels.values() if n["node_type"] == "goal"]
    assert len(goal_nodes) == 1


def test_duplicate_goal_texts_collapse_to_one_node(conn):
    """Re-extraction mints new goal_ids for the same goal text — the graph
    must key goal nodes by text so duplicates merge, with the edge window
    spanning earliest→latest occurrence."""
    owner = _owner(conn)
    for i, (rid, day) in enumerate([("r1", "2025-03-01"), ("r2", "2025-04-01"), ("r3", "2025-05-01")]):
        conn.execute(
            "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
            f"VALUES ('dup{i}', '{rid}', 'chatgpt_file_ingestion', 'Deepen Orion coverage', '{{}}')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table) "
            f"VALUES ('{day}T09:00:00Z', '{rid}', 'chatgpt_file_ingestion', 'ai_chat_messages')"
        )
    conn.commit()

    materialize_graph_enrichments(conn)
    labels = _labels(conn)
    goal_nodes = [n for n in labels.values() if n["node_type"] == "goal"]
    assert len(goal_nodes) == 1  # three extractions, one goal
    pursues = _edges(conn, "pursues")
    assert len(pursues) == 1
    assert pursues[0]["src_node_id"] == owner
    assert str(pursues[0]["valid_from"]).startswith("2025-03-01")   # earliest
    assert str(pursues[0]["last_event_at"]).startswith("2025-05-01")  # latest


# ------------------------------------------------------------------ places


def test_visits_become_located_at_edges_weighted_by_count(conn):
    owner = _owner(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
            f"VALUES ('l{i}', 'Metro Fitness', '2026-06-0{i + 1}T10:00:00Z', 'grow_journal')"
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
    assert labels["Metro Fitness"]["node_type"] == "place"


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


def test_retracted_goal_edge_survives_refresh_until_swept(conn):
    """A goal deleted at source keeps its edge through the refresh itself and
    loses it only in the end-of-rebuild sweep — the old lifecycle (wipe all mz
    edges up front) served a goal-less graph for the whole enricher window."""
    from topos.features.entities.fact_materializer import sweep_stale_materialized_edges

    _owner(conn)
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
        "VALUES ('g_keep', 'rec-1', 'chatgpt_file_ingestion', 'Keep shipping the pilot', '{}')"
    )
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) "
        "VALUES ('g_drop', 'rec-2', 'chatgpt_file_ingestion', 'Attend the Q2 offsite', '{}')"
    )
    conn.commit()

    touched_first: set = set()
    materialize_graph_enrichments(conn, touched_edges=touched_first)
    assert len(_edges(conn, "pursues")) == 2
    assert len(touched_first) == 2

    conn.execute("DELETE FROM user_goals WHERE goal_id='g_drop'")
    conn.commit()

    touched_second: set = set()
    materialize_graph_enrichments(conn, touched_edges=touched_second)
    # The retracted goal's edge is still live after the refresh (no missing
    # window) — only the sweep, fed by what THIS run touched, removes it.
    assert len(_edges(conn, "pursues")) == 2
    assert len(touched_second) == 1

    swept = sweep_stale_materialized_edges(conn, touched_second)
    conn.commit()
    assert swept == 1
    remaining = _edges(conn, "pursues")
    assert len(remaining) == 1
    assert "Keep shipping the pilot" in json.loads(remaining[0]["metadata_json"])["statement"]
