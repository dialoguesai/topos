"""G5-2 — the candidate half of the bench, answered with what the record supports.

The request asks for people whose demonstrated work maps to a role. That cannot be answered
on this node: zero `net.demonstrated_skill` facts, and the person-to-work-cluster join
returns zero pairs because roles come from commits and people are known through
conversations, which the clustering puts in disjoint dimensions.

What is answerable: whose conversations sit near the owner's own work. These tests pin that
it stays that claim and does not drift into the stronger one.
"""

from __future__ import annotations

import sqlite3
import struct

import pytest

from topos.features.derivation.social_bench import (
    ENGAGEMENT_TOP_K,
    MIN_ENGAGEMENT_MESSAGES,
    work_engagement,
)


def _blob(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "e.db"))
    c.executescript("""
      CREATE TABLE signal_embeddings (record_id TEXT, source_id TEXT, record_type TEXT,
        vector_blob BLOB, vector_format TEXT);
      CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
        member_count INTEGER, label_terms_json TEXT);
      CREATE TABLE topic_cluster_members (member_id TEXT PRIMARY KEY, cluster_id TEXT,
        record_id TEXT);
      -- the person graph's own substrate: this read is only as good as who it knows
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER,
        contact_id TEXT);
      CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT, dst_entity_id TEXT,
        edge_type TEXT, weight REAL, metadata_json TEXT);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, authored_by_owner INTEGER);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT,
        identifier_type TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, payload_json TEXT,
        confidence REAL, source_refs_json TEXT);
      CREATE TABLE messenger_social_edges (dataset_id TEXT, period_key TEXT,
        source_scope TEXT, source_id TEXT, target_id TEXT, weight REAL, edge_type TEXT);
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT PRIMARY KEY,
        content TEXT, is_from_self INTEGER, conversation_id TEXT, sender_id TEXT,
        event_at TEXT, source_id TEXT, reply_to_message_id TEXT);
    """)
    yield c
    c.close()


def _work(conn, n=6):
    conn.execute("INSERT INTO topic_clusters VALUES ('w1','Engine Relay','work',?,'[]')", (n,))
    for i in range(n):
        rid = f"github:acme/engine:{i:040d}"
        conn.execute("INSERT INTO topic_cluster_members VALUES (?,?,?)", (f"m{i}", "w1", rid))
        conn.execute("INSERT INTO signal_embeddings VALUES (?,?,?,?,?)",
                     (rid, "github_activity", "activity_event", _blob([1.0, 0.0, 0.0]), "f32"))


def _talk(conn, key, n, vector, msgs=None):
    """`n` messages in a conversation with `key`, all pointing the same way.

    Also registers the peer as a messaging dyad, because this read only ranks people the
    person graph already knows — that is the point of it, not an incidental dependency.
    """
    conn.execute("INSERT INTO messenger_dyad_stats VALUES ('ds','self',?,1,'human',?)",
                 (key, msgs if msgs is not None else n))
    for i in range(n):
        mid = f"msg-{key}-{i}"
        conn.execute(
            "INSERT INTO conversation_messages VALUES ('ds',?,?,0,?,?,?,'imessage',NULL)",
            (mid, "hello", f"conv-{key}", key, f"2026-01-{(i % 27) + 1:02d}T09:00:00"))
        conn.execute("INSERT INTO signal_embeddings VALUES (?,?,?,?,?)",
                     (mid, "imessage", "conversation_message", _blob(vector), "f32"))


def test_someone_talking_about_the_work_outranks_someone_who_is_not(conn):
    _work(conn)
    _talk(conn, "+1111", MIN_ENGAGEMENT_MESSAGES + 5, [1.0, 0.0, 0.0])   # on the work
    _talk(conn, "+2222", MIN_ENGAGEMENT_MESSAGES + 5, [0.0, 1.0, 0.0])   # orthogonal
    conn.commit()
    out = work_engagement(conn, "ds")
    by = {p["node_id"]: p for p in out["people"]}
    assert by, "both clear the message floor"
    on = next(p for p in out["people"] if "1111" in str(p["node_id"]))
    off = next(p for p in out["people"] if "2222" in str(p["node_id"]))
    assert on["engagement"] > off["engagement"]


def test_a_thin_correspondent_is_not_scored_at_all(conn):
    """Below the floor a single stray message about a deployment decides the ranking."""
    _work(conn)
    _talk(conn, "+3333", MIN_ENGAGEMENT_MESSAGES - 1, [1.0, 0.0, 0.0])
    conn.commit()
    out = work_engagement(conn, "ds")
    assert all("3333" not in str(p["node_id"]) for p in out["people"])


def test_it_reports_engagement_not_capability(conn):
    """The whole point of the separation: this says who is NEAR the work, never who can do
    it. A report that blurs those has answered a question it cannot answer."""
    _work(conn)
    _talk(conn, "+1111", MIN_ENGAGEMENT_MESSAGES + 2, [1.0, 0.0, 0.0])
    conn.commit()
    out = work_engagement(conn, "ds")
    assert "NOT people evidenced to be able to do it" in out["coverage"]["means"]
    assert "separation of 0.043" in out["coverage"]["why_not_per_role"] \
        or "0.043" in out["coverage"]["why_not_per_role"]
    for person in out["people"]:
        assert "skill" not in str(person.get("basis", "")).lower()


def test_warmth_decides_the_order_engagement_decides_the_list(conn):
    """The request is explicit: ordered by warmth rather than fit, because a warm
    second-best is worth more than a cold ideal."""
    _work(conn)
    _talk(conn, "+1111", MIN_ENGAGEMENT_MESSAGES + 2, [1.0, 0.0, 0.0])
    _talk(conn, "+2222", MIN_ENGAGEMENT_MESSAGES + 2, [0.9, 0.44, 0.0])
    conn.commit()
    out = work_engagement(conn, "ds")
    assert "warmth" in out["coverage"]["ordered_by"]
    warmths = [p["closeness"] or 0 for p in out["people"]]
    assert warmths == sorted(warmths, reverse=True), "warmth first, not engagement"


def test_no_embeddings_is_stated_not_guessed(conn):
    conn.execute("INSERT INTO topic_clusters VALUES ('w1','Engine Relay','work',0,'[]')")
    conn.commit()
    out = work_engagement(conn, "ds")
    assert out["people"] == []
    assert "reason" in out["coverage"]


def test_an_empty_node_is_not_blamed_on_the_message_floor(conn):
    """Two very different silences. Reporting "nobody talks enough" on a node with no people
    at all sends the reader looking for a threshold problem that is not there."""
    _work(conn)
    # conversations exist and are embedded — but no dyad, so the person graph knows nobody
    for i in range(MIN_ENGAGEMENT_MESSAGES + 5):
        conn.execute(
            "INSERT INTO conversation_messages VALUES ('ds',?,?,0,?,?,?,'imessage',NULL)",
            (f"orphan-{i}", "hello", "conv-orphan", "+9999",
             f"2026-01-{(i % 27) + 1:02d}T09:00:00"))
        conn.execute("INSERT INTO signal_embeddings VALUES (?,?,?,?,?)",
                     (f"orphan-{i}", "imessage", "conversation_message",
                      _blob([1.0, 0.0, 0.0]), "f32"))
    conn.commit()
    out = work_engagement(conn, "ds")
    assert out["people"] == []
    assert "no messaging people are known" in out["coverage"]["reason"]


def test_floors_are_what_the_docstrings_say():
    assert MIN_ENGAGEMENT_MESSAGES == 15
    assert ENGAGEMENT_TOP_K == 5
