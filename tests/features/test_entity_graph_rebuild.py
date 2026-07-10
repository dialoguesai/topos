"""Entity-graph backfill: rebuild evidence edges from surviving mentions.

Backs the "rebuild co-occurrence" maintenance path. Two properties matter:
  * co_occurrence is recomputed from mention pairs per record;
  * communicates_with is REBUILT (the historical scrub bug deleted it and never
    recreated it — after any source deletion those edges vanished permanently).
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import graph_snapshot
from topos.features.entities.maintenance import (
    rebuild_entity_graph,
    rebuild_evidence_edges,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _mention(conn, *, entity_id, record_id, table="conversation_messages", event_at="2026-01-01T00:00:00Z"):
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table,
             surface_text, confidence, event_at, created_at)
        VALUES (?, ?, ?, 'imessage', ?, 'x', 0.9, ?, ?)
        """,
        (f"m_{entity_id}_{record_id}", entity_id, record_id, table, event_at, event_at),
    )


def _seed_two_entities_one_record(conn):
    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Bram", "person")
    conn.commit()
    _mention(conn, entity_id=a, record_id="rec1")
    _mention(conn, entity_id=b, record_id="rec1")
    conn.commit()
    return a, b


def test_co_occurrence_rebuilt_from_mentions(conn):
    a, b = _seed_two_entities_one_record(conn)
    conn.execute("DELETE FROM entity_edges")
    conn.commit()
    stats = rebuild_evidence_edges(conn)
    assert stats["co_occurrence"] >= 1
    pairs = {(e["src_node_id"], e["dst_node_id"]) for e in graph_snapshot(conn)["edges"]}
    assert {(a, b), (b, a)} & pairs


def test_communicates_with_is_rebuilt_not_dropped(conn):
    """The bug: rebuild must (re)create communicates_with, not just delete it."""
    a, b = _seed_two_entities_one_record(conn)
    conn.execute("DELETE FROM entity_edges")
    conn.commit()
    # rec1 was authored by a sender; the rebuild resolves it and links sender→entity.
    stats = rebuild_evidence_edges(conn, sender_lookup=lambda _c, _t: {"rec1": "Carol"})
    assert stats["communicates_with"] >= 1
    types = {e["edge_type"] for e in graph_snapshot(conn)["edges"]}
    assert "communicates_with" in types


def test_rebuild_is_idempotent(conn):
    _seed_two_entities_one_record(conn)
    first = rebuild_evidence_edges(conn, sender_lookup=lambda _c, _t: {"rec1": "Carol"})
    active_after_first = conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL"
    ).fetchone()[0]
    second = rebuild_evidence_edges(conn, sender_lookup=lambda _c, _t: {"rec1": "Carol"})
    active_after_second = conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL"
    ).fetchone()[0]
    assert first == second
    assert active_after_first == active_after_second


def test_edge_metadata_carries_provenance_role_mix(conn):
    """Evidence edges are stamped with their records' provenance roles.

    A conversation_messages record with actor_role='observed' (witnessed
    speech) must yield an edge whose metadata says observed — the graph's
    personal→ambient attribution overlay reads this.
    """
    import json as _json

    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Bram", "person")
    conn.commit()
    # canonical message tables are created by ingestion, not migrations —
    # the minimal shape the role lookup reads is enough here.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_messages ("
        "message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_type TEXT, "
        "sender_id TEXT, is_from_self INTEGER, actor_role TEXT)"
    )
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, sender_type, actor_role) "
        "VALUES ('m1', 'c1', 'human', 'observed')"
    )
    _mention(conn, entity_id=a, record_id="m1", table="conversation_messages")
    _mention(conn, entity_id=b, record_id="m1", table="conversation_messages")
    conn.commit()

    rebuild_evidence_edges(conn)
    meta = _json.loads(
        conn.execute(
            "SELECT metadata_json FROM entity_edges WHERE edge_type='co_occurrence' AND valid_to IS NULL"
        ).fetchone()[0]
        or "{}"
    )
    assert meta.get("actor_role") == "observed"
    assert meta.get("role_mix") == {"observed": 1}

    # And the API read path must EXPOSE it — graph_snapshot historically
    # synthesized metadata_json and dropped the stored column entirely.
    snap = graph_snapshot(conn, min_weight=0.0)
    api_meta = _json.loads(snap["edges"][0]["metadata_json"] or "{}")
    assert api_meta.get("actor_role") == "observed"
    assert api_meta.get("evidence_count") is not None  # synthesized fields kept


def test_journal_evidence_is_authored(conn):
    """Journal records are owner-authored by construction → edge role authored."""
    import json as _json

    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Yoga", "topic")
    conn.commit()
    conn.execute(
        "INSERT INTO journal_entries (entry_id, source_id, content) VALUES ('j1', 'grow_journal', 'did yoga with Ada')"
    )
    _mention(conn, entity_id=a, record_id="j1", table="journal_entries")
    _mention(conn, entity_id=b, record_id="j1", table="journal_entries")
    conn.commit()

    rebuild_evidence_edges(conn)
    meta = _json.loads(
        conn.execute(
            "SELECT metadata_json FROM entity_edges WHERE edge_type='co_occurrence' AND valid_to IS NULL"
        ).fetchone()[0]
        or "{}"
    )
    assert meta.get("actor_role") == "authored"


def test_communities_detected_and_exposed(conn):
    """Louvain neighborhoods (the witcher-network recipe): two disconnected
    cliques must land in different communities, exposed via node metadata."""
    import json as _json

    from topos.features.entities.maintenance import compute_communities

    r = EntityResolver(conn)
    clique_a = [r._create_entity(n, "person") for n in ("Ada", "Bram", "Cass")]
    clique_b = [r._create_entity(n, "org") for n in ("Xen", "Yara", "Zed")]
    conn.commit()
    from topos.features.entities.edges import update_edge

    for group in (clique_a, clique_b):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                update_edge(conn, src_entity_id=group[i], dst_entity_id=group[j],
                            edge_type="co_occurrence", event_at="2026-07-01T00:00:00Z")
    conn.commit()

    out = compute_communities(conn)
    assert out["communities"] >= 2
    assert out["nodes_labeled"] == 6

    def _community(eid):
        meta = _json.loads(conn.execute(
            "SELECT metadata_json FROM entities WHERE entity_id=?", (eid,)
        ).fetchone()[0] or "{}")
        return meta.get("community_id")

    a_comms = {_community(e) for e in clique_a}
    b_comms = {_community(e) for e in clique_b}
    assert len(a_comms) == 1 and len(b_comms) == 1  # cliques are cohesive
    assert a_comms != b_comms  # and separated

    # The API read path exposes community_id on node metadata.
    snap = graph_snapshot(conn, min_weight=0.0)
    node_meta = _json.loads(snap["nodes"][0]["metadata_json"] or "{}")
    assert "community_id" in node_meta
    assert "mention_count" in node_meta  # synthesized field kept


def test_rebuild_entity_graph_reports_before_after(conn):
    _seed_two_entities_one_record(conn)
    conn.execute("DELETE FROM entity_edges")
    conn.commit()
    report = rebuild_entity_graph(conn)
    assert report["edges_before"] == 0
    assert report["edges_after"] >= 1
    assert "co_occurrence" in report and "communicates_with" in report
