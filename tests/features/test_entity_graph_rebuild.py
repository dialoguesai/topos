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


def test_rebuild_entity_graph_reports_before_after(conn):
    _seed_two_entities_one_record(conn)
    conn.execute("DELETE FROM entity_edges")
    conn.commit()
    report = rebuild_entity_graph(conn)
    assert report["edges_before"] == 0
    assert report["edges_after"] >= 1
    assert "co_occurrence" in report and "communicates_with" in report
