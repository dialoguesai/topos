"""Materialize facts + topic clusters from signal_objects into the entity graph.

The temporal context graph the owner expects: their extracted statements
(signal_objects) become labeled, bi-temporal edges in the entity spine so the
existing /entities/graph endpoint + UI render them with no frontend change.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.edges import graph_snapshot
from topos.features.entities.fact_materializer import (
    materialize_signal_objects_to_graph,
    sweep_stale_materialized_edges,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _put_object(conn, *, object_id, object_type, payload, object_key="k", valid_from="2026-01-01T00:00:00Z", valid_to=None, confidence=0.8):
    conn.execute(
        """
        INSERT INTO signal_objects
            (object_id, signal_dimension, object_type, object_key, payload_json,
             confidence, source_refs_json, valid_from, valid_to, extractor_version,
             created_at, updated_at, created_by, updated_by)
        VALUES (?, 'memory', ?, ?, ?, ?, '[]', ?, ?, 'test', ?, ?, 'system', 'system')
        """,
        (object_id, object_type, object_key, json.dumps(payload), confidence,
         valid_from, valid_to, valid_from, valid_from),
    )
    conn.commit()


def _edge_types(snap):
    return [e["edge_type"] for e in snap["edges"]]


def test_topic_cluster_becomes_hub_with_discusses_edges(conn):
    r = EntityResolver(conn)
    r._create_entity("Google", "org")
    conn.commit()
    _put_object(
        conn,
        object_id="o1",
        object_type="top_topics",
        object_key="tc_abc",
        payload={"tag": "search / google / analytics", "related_entities": ["Google", "PostHog"]},
    )
    out = materialize_signal_objects_to_graph(conn)
    assert out["topic_edges"] >= 1
    snap = graph_snapshot(conn, min_weight=0.0)
    assert "discusses" in _edge_types(snap)
    # a topic hub node was created and is present on an edge
    labels = {n["label"] for n in snap["nodes"]}
    assert any("google" in str(l).lower() for l in labels)


def test_fact_triple_becomes_labeled_edge(conn):
    r = EntityResolver(conn)
    subj = r._create_entity("Jonny", "person")
    # L4-8: subject facts project only for the OWNER; the guard fails closed otherwise
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (subj,))
    conn.commit()
    _put_object(
        conn,
        object_id="f1",
        object_type="fact",
        object_key="fact:jonny:works_at",
        payload={
            "subject_entity_id": subj,
            "predicate": "works_at",
            "object_value": "Dialogues",
            "confidence": 0.9,
            "asserted_by": "owner",
        },
    )
    out = materialize_signal_objects_to_graph(conn)
    assert out["fact_edges"] >= 1
    snap = graph_snapshot(conn, min_weight=0.0)
    # object string was resolved to a node (per the resolve-to-nodes choice)
    labels = {str(n["label"]).lower() for n in snap["nodes"]}
    assert any("dialogues" in l for l in labels)
    # edge weight is above the co-occurrence noise floor so it shows by default
    assert any(e["weight"] >= 1.5 for e in snap["edges"])


def test_materialize_is_idempotent(conn):
    r = EntityResolver(conn)
    subj = r._create_entity("Jonny", "person")
    # L4-8: subject facts project only for the OWNER; the guard fails closed otherwise
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (subj,))
    conn.commit()
    _put_object(
        conn, object_id="f1", object_type="fact", object_key="fact:jonny:lived_in",
        payload={"subject_entity_id": subj, "predicate": "lived_in", "object_value": "Brooklyn", "confidence": 0.8},
    )
    first = materialize_signal_objects_to_graph(conn)
    n1 = conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    second = materialize_signal_objects_to_graph(conn)
    n2 = conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    assert first == second
    assert n1 == n2


def test_junk_related_entities_are_skipped(conn):
    _put_object(
        conn, object_id="o2", object_type="top_topics", object_key="tc_junk",
        payload={"tag": "noise", "related_entities": ["##I", "G", "##ny"]},
    )
    out = materialize_signal_objects_to_graph(conn)
    # all related entities are NER fragments → no edges
    assert out["topic_edges"] == 0


def test_discusses_edges_use_member_activity_not_object_insert_time(conn):
    """Active-in must date topics by member event time (like goals), not the
    signal_objects.valid_from stamp from first insert — otherwise a live
    cluster looks dead (or vice versa) and the graph flips topic-only/goal-only.
    """
    from datetime import datetime, timedelta, timezone

    r = EntityResolver(conn)
    r._create_entity("Google", "org")
    conn.commit()

    now = datetime.now(timezone.utc)
    stale_from = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_at = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    older_at = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put_object(
        conn,
        object_id="o_stale",
        object_type="top_topics",
        object_key="tc_live",
        payload={"tag": "search analytics", "related_entities": ["Google"]},
        valid_from=stale_from,
    )
    conn.execute(
        "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count) "
        "VALUES ('tc_live', 'search analytics', 'memory', 2)"
    )
    conn.execute(
        "INSERT INTO topic_cluster_members "
        "(member_id, cluster_id, record_id, source_id, record_type, weight) "
        "VALUES ('m1', 'tc_live', 'rec-recent', 'chatgpt_file_ingestion', 'message', 1.0)"
    )
    conn.execute(
        "INSERT INTO topic_cluster_members "
        "(member_id, cluster_id, record_id, source_id, record_type, weight) "
        "VALUES ('m2', 'tc_live', 'rec-old', 'chatgpt_file_ingestion', 'message', 1.0)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table) "
        "VALUES (?, 'rec-recent', 'chatgpt_file_ingestion', 'ai_chat_messages')",
        (recent_at,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table) "
        "VALUES (?, 'rec-old', 'chatgpt_file_ingestion', 'ai_chat_messages')",
        (older_at,),
    )
    conn.commit()

    out = materialize_signal_objects_to_graph(conn)
    assert out["topic_edges"] >= 1

    discusses = [
        e for e in graph_snapshot(conn, min_weight=0.0)["edges"]
        if e["edge_type"] == "discusses"
    ]
    assert discusses, "expected discusses edges"
    # Window spans earliest→latest member activity, not the stale object insert.
    assert str(discusses[0]["valid_from"]).startswith(older_at[:10])
    assert str(discusses[0]["last_event_at"]).startswith(recent_at[:10])

    # 14-day Active-in style filter (UI: last_event_at || valid_from).
    window_start = now - timedelta(days=14)
    kept = []
    for edge in discusses:
        stamp = edge.get("last_event_at") or edge.get("valid_from") or ""
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if window_start <= t <= now + timedelta(days=1):
            kept.append(edge)
    assert kept, "recent member activity should keep discusses inside a 14d Active-in window"


def test_rematerialize_updates_edges_in_place_never_deletes_first(conn):
    """Upsert-then-sweep: a re-run must UPDATE surviving edges, not wipe and
    re-mint them — the old up-front wipe left the committed graph without its
    materialized edges for the minutes the slow enricher lanes take, so every
    /entities/graph read in that window rendered a goal-less graph."""
    r = EntityResolver(conn)
    subj = r._create_entity("Jonny", "person")
    # L4-8: subject facts project only for the OWNER; the guard fails closed otherwise
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (subj,))
    conn.commit()
    _put_object(
        conn, object_id="f1", object_type="fact", object_key="fact:jonny:works_at",
        payload={"subject_entity_id": subj, "predicate": "works_at", "object_value": "Dialogues", "confidence": 0.9},
    )
    touched_first: set = set()
    materialize_signal_objects_to_graph(conn, touched_edges=touched_first)
    ids_first = {
        str(row[0])
        for row in conn.execute(
            "SELECT edge_id FROM entity_edges WHERE json_extract(metadata_json,'$.mz')=1"
        )
    }
    assert ids_first and touched_first == ids_first

    touched_second: set = set()
    materialize_signal_objects_to_graph(conn, touched_edges=touched_second)
    ids_second = {
        str(row[0])
        for row in conn.execute(
            "SELECT edge_id FROM entity_edges WHERE json_extract(metadata_json,'$.mz')=1"
        )
    }
    # Same rows, updated in place: stable ids prove nothing was dropped+re-minted.
    assert ids_second == ids_first
    assert touched_second == ids_first


def test_sweep_removes_only_stale_mz_edges(conn):
    r = EntityResolver(conn)
    subj = r._create_entity("Jonny", "person")
    org = r._create_entity("Dialogues", "org")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (subj,))
    conn.commit()
    # An organic evidence edge (no mz tag) must never be sweep-eligible.
    conn.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, "
        "weight, evidence_count, valid_from) VALUES ('org1', ?, ?, 'co_occurrence', 1.0, 1, "
        "'2026-01-01T00:00:00Z')",
        (subj, org),
    )
    _put_object(
        conn, object_id="f1", object_type="fact", object_key="fact:jonny:works_at",
        payload={"subject_entity_id": subj, "predicate": "works_at", "object_value": "Dialogues", "confidence": 0.9},
    )
    touched: set = set()
    materialize_signal_objects_to_graph(conn, touched_edges=touched)
    assert len(touched) == 1

    # Fact retracted at source: the refresh no longer touches its edge, but the
    # edge stays live until the sweep — readers never see a missing window.
    conn.execute("UPDATE signal_objects SET valid_to='2026-02-01T00:00:00Z' WHERE object_id='f1'")
    conn.commit()
    touched_after: set = set()
    materialize_signal_objects_to_graph(conn, touched_edges=touched_after)
    assert touched_after == set()
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE json_extract(metadata_json,'$.mz')=1"
    ).fetchone()[0] == 1

    swept = sweep_stale_materialized_edges(conn, touched_after)
    conn.commit()
    assert swept == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE json_extract(metadata_json,'$.mz')=1"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE edge_id='org1'"
    ).fetchone()[0] == 1
