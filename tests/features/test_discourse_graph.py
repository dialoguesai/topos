"""Discourse lenses mint recording hubs + nugget nodes + predicated edges."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.discourse_graph import (
    EDGE_ABOUT,
    EDGE_WINDOWED,
    discourse_enabled_source_ids,
    materialize_discourse_lenses_to_graph,
)
from topos.features.entities.edges import graph_snapshot
from topos.features.entities.resolver import EntityResolver
from topos.sources.definitions import source_gets_discourse_lenses
from topos.sources.registry import DEMO_JOURNAL_FILE, YOUTUBE_TRANSCRIPTS, discourse_lens_source_ids
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "d.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_transcript(conn: sqlite3.Connection) -> tuple[str, str, str]:
    resolver = EntityResolver(conn)
    white_house = resolver._create_entity("the White House", "org")
    eric = resolver._create_entity("Eric", "person")
    conn.execute(
        """
        INSERT INTO transcripts (transcript_id, title, origin_kind, participation_mode, asr_quality, source_id, source_record_id)
        VALUES ('yt:demo', 'Demo podcast', 'youtube', 'ambient', 'generated', 'youtube_transcripts', 'yt:demo')
        """
    )
    rows = [
        ("yt:demo:0", "We argued in our meeting with the White House on AI policy that this is a problem.", 0.0),
        ("yt:demo:45", "And you start to wonder is Eric Weinstein right because physics went down a certain road.", 45.0),
        ("yt:demo:90", "DARPA funded the remote viewing program after the hearing in 1972.", 90.0),
    ]
    for sid, text, start in rows:
        conn.execute(
            """
            INSERT INTO transcript_segments (
                segment_id, transcript_id, content, start_sec, event_at,
                actor_role, is_from_self, source_id, source_record_id
            ) VALUES (?, 'yt:demo', ?, ?, '2026-06-01T10:00:00Z', 'ambient', 0, 'youtube_transcripts', ?)
            """,
            (sid, text, start, sid),
        )
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES ('m1', ?, 'yt:demo:0', 'youtube_transcripts', 'White House', 0.9, '2026-06-01')",
        (white_house,),
    )
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES ('m2', ?, 'yt:demo:45', 'youtube_transcripts', 'Eric', 0.9, '2026-06-01')",
        (eric,),
    )
    conn.commit()
    return white_house, eric, "transcript:yt:demo"


def _edges(conn, edge_type):
    snap = graph_snapshot(conn, min_weight=0.0)
    return [e for e in snap["edges"] if e["edge_type"] == edge_type]


def test_claim_hangs_off_recording_and_points_at_names(conn):
    white_house, _eric, rec_id = _seed_transcript(conn)
    out = materialize_discourse_lenses_to_graph(conn)
    assert out["recordings"] == 1
    assert out["claims"] >= 1

    snap = graph_snapshot(conn, min_weight=0.0)
    types = {n["node_type"] for n in snap["nodes"]}
    assert "document" in types
    assert "claim" in types

    discusses = _edges(conn, "discusses")
    assert any(e["src_node_id"] == rec_id for e in discusses)

    about = _edges(conn, EDGE_ABOUT)
    assert any(e["dst_node_id"] == white_house for e in about)
    claim_ids = {n["node_id"] for n in snap["nodes"] if n["node_type"] == "claim"}
    assert claim_ids
    for cid in claim_ids:
        assert any(e["src_node_id"] == rec_id and e["dst_node_id"] == cid for e in discusses)

    rec_meta = json.loads(
        next(n["metadata_json"] for n in snap["nodes"] if n["node_id"] == rec_id) or "{}"
    )
    assert rec_meta.get("source_id") == "youtube_transcripts"
    about_meta = json.loads(about[0]["metadata_json"] or "{}")
    assert about_meta.get("source_id") == "youtube_transcripts"
    wh_meta = json.loads(
        next(n["metadata_json"] for n in snap["nodes"] if n["node_id"] == white_house) or "{}"
    )
    assert "youtube_transcripts" in (wh_meta.get("source_ids") or [])


def test_program_and_event_nodes_mint(conn):
    _seed_transcript(conn)
    out = materialize_discourse_lenses_to_graph(conn)
    assert out["programs"] >= 1
    assert out["events"] >= 1
    snap = graph_snapshot(conn, min_weight=0.0)
    types = {n["node_type"] for n in snap["nodes"]}
    assert "program" in types
    assert "event" in types


def test_windowed_relation_links_names_across_captions(conn):
    white_house, eric, _rec = _seed_transcript(conn)
    conn.execute(
        "UPDATE transcript_segments SET start_sec=20.0 WHERE segment_id='yt:demo:45'"
    )
    conn.commit()
    materialize_discourse_lenses_to_graph(conn)
    windowed = _edges(conn, EDGE_WINDOWED)
    pair = {(e["src_node_id"], e["dst_node_id"]) for e in windowed}
    assert (white_house, eric) in pair or (eric, white_house) in pair


def test_discourse_lenses_bind_to_transcripts_group_not_journals():
    assert YOUTUBE_TRANSCRIPTS.canonical_group_id == "transcripts"
    assert source_gets_discourse_lenses(YOUTUBE_TRANSCRIPTS) is True
    assert source_gets_discourse_lenses(DEMO_JOURNAL_FILE) is False
    # Flag cannot opt a journal in.
    assert (
        source_gets_discourse_lenses(
            {
                "source_id": "grow_journal",
                "canonical_group_id": "journal",
                "discourse_lenses": True,
            }
        )
        is False
    )
    # Meetings / lectures opt in by landing on the transcripts lane.
    assert (
        source_gets_discourse_lenses(
            {"source_id": "meetings", "canonical_group_id": "transcripts"}
        )
        is True
    )
    assert (
        source_gets_discourse_lenses(
            {
                "source_id": "meetings",
                "canonical_group_id": "transcripts",
                "discourse_lenses": False,
            }
        )
        is False
    )
    assert "youtube_transcripts" in discourse_lens_source_ids()
    assert "demo_journal_file" not in discourse_lens_source_ids()


def test_journal_entry_does_not_mint_claims_but_does_discuss_topics(conn):
    white_house, _eric, _rec = _seed_transcript(conn)
    resolver = EntityResolver(conn)
    mood = resolver._create_entity("PrivateMood", "person")
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, entry_at, content, source_id, source_record_id)
        VALUES (
            'je-1',
            '2026-06-01T10:00:00Z',
            'The fact is I am exhausted because this is a problem and I argued with myself all night.',
            'demo_journal_file',
            'je-1'
        )
        """
    )
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text, confidence, created_at) "
        "VALUES ('mj1', ?, 'je-1', 'demo_journal_file', 'PrivateMood', 0.9, '2026-06-01')",
        (mood,),
    )
    conn.execute(
        "INSERT INTO topic_clusters (cluster_id, label) VALUES ('c1', 'mixed cluster')"
    )
    conn.execute(
        """
        INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id, record_type)
        VALUES
          ('tm-j', 'c1', 'je-1', 'demo_journal_file', 'journal'),
          ('tm-yt', 'c1', 'yt:demo:0', 'youtube_transcripts', 'transcript')
        """
    )
    conn.commit()

    out = materialize_discourse_lenses_to_graph(conn)
    assert out["claims"] >= 1
    assert "demo_journal_file" not in discourse_enabled_source_ids(conn)

    claim_segs = {
        json.loads(row[0] or "{}").get("segment_id")
        for row in conn.execute(
            "SELECT metadata_json FROM entities WHERE entity_type='claim'"
        )
    }
    assert "je-1" not in claim_segs

    discusses = _edges(conn, "discusses")
    topic_to_mood = [
        e for e in discusses if e["src_node_id"].startswith("topic_") and e["dst_node_id"] == mood
    ]
    assert topic_to_mood, "journals belong on topic hubs; that is not a discourse leak"
    topic_to_wh = [
        e
        for e in discusses
        if e["src_node_id"].startswith("topic_") and e["dst_node_id"] == white_house
    ]
    assert topic_to_wh


def test_journal_source_id_on_caption_is_skipped(conn):
    _seed_transcript(conn)
    conn.execute(
        """
        INSERT INTO transcript_segments (
            segment_id, transcript_id, content, start_sec, event_at,
            actor_role, is_from_self, source_id, source_record_id
        ) VALUES (
            'je-seg', 'yt:demo',
            'The fact is I am exhausted because this is a problem and I argued with myself all night about it.',
            12.0, '2026-06-01T10:00:00Z', 'ambient', 0, 'demo_journal_file', 'je-seg'
        )
        """
    )
    conn.commit()
    materialize_discourse_lenses_to_graph(conn)
    claim_segs = {
        json.loads(row[0] or "{}").get("segment_id")
        for row in conn.execute(
            "SELECT metadata_json FROM entities WHERE entity_type='claim'"
        )
    }
    assert "je-seg" not in claim_segs
    assert any(sid and str(sid).startswith("yt:demo:") for sid in claim_segs)


def test_future_meetings_source_on_transcripts_lane_mints(conn):
    resolver = EntityResolver(conn)
    resolver._create_entity("Acme", "org")
    conn.execute(
        """
        INSERT INTO transcripts (transcript_id, title, origin_kind, participation_mode, asr_quality, source_id, source_record_id)
        VALUES ('call:1', 'Sales call', 'meeting', 'ambient', 'generated', 'sales_calls', 'call:1')
        """
    )
    conn.execute(
        """
        INSERT INTO transcript_segments (
            segment_id, transcript_id, content, start_sec, event_at,
            actor_role, is_from_self, source_id, source_record_id
        ) VALUES (
            'call:1:0', 'call:1',
            'We argued that the fact is Acme should buy because this is a problem for their team this quarter.',
            0.0, '2026-06-01T10:00:00Z', 'ambient', 0, 'sales_calls', 'call:1:0'
        )
        """
    )
    conn.commit()
    assert "sales_calls" in discourse_enabled_source_ids(conn)
    out = materialize_discourse_lenses_to_graph(conn)
    assert out["recordings"] == 1
    assert out["claims"] >= 1


def test_transcripts_source_can_opt_out_of_discourse(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_runtime_installs (
            source_id TEXT,
            source_definition_json TEXT,
            is_active INTEGER,
            status TEXT
        )
        """
    )
    payload = {
        "source_id": "meetings_off",
        "display_name": "Meetings (no lenses)",
        "source_type": "file",
        "schema_id": "transcript.session.v1",
        "parser_id": "transcript.session.v1",
        "canonical_group_id": "transcripts",
        "discourse_lenses": False,
    }
    conn.execute(
        "INSERT INTO source_runtime_installs (source_id, source_definition_json, is_active, status) "
        "VALUES ('meetings_off', ?, 1, 'installed')",
        (json.dumps(payload),),
    )
    conn.execute(
        """
        INSERT INTO transcripts (transcript_id, title, origin_kind, participation_mode, asr_quality, source_id, source_record_id)
        VALUES ('mtg:1', 'Standup', 'meeting', 'ambient', 'generated', 'meetings_off', 'mtg:1')
        """
    )
    conn.execute(
        """
        INSERT INTO transcript_segments (
            segment_id, transcript_id, content, start_sec, event_at,
            actor_role, is_from_self, source_id, source_record_id
        ) VALUES (
            'mtg:1:0', 'mtg:1',
            'The fact is this standup is a problem because we argued for an hour about nothing in particular.',
            0.0, '2026-06-01T10:00:00Z', 'ambient', 0, 'meetings_off', 'mtg:1:0'
        )
        """
    )
    conn.commit()
    assert "meetings_off" not in discourse_enabled_source_ids(conn)
    out = materialize_discourse_lenses_to_graph(conn)
    assert out["recordings"] == 0
    assert out["claims"] == 0


def test_discourse_edges_excluded_from_relationship_context_graph_lane(conn):
    from topos.query.graph_lane import graph_neighborhood_items
    from topos.query.manifest_validation import resolve_scope_manifest

    white_house, eric, _rec = _seed_transcript(conn)
    materialize_discourse_lenses_to_graph(conn)
    discourse_types = {"windowed_with", "about", "discusses"}
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT edge_type FROM entity_edges WHERE edge_type IN ('windowed_with','about','discusses')"
        )
    }
    assert stored, "fixture must mint at least one discourse edge"

    rel_manifest = resolve_scope_manifest("relationship_context:read")
    rel_items = graph_neighborhood_items(
        conn,
        anchor_ids=[white_house, eric],
        anchor_names={white_house: "White House", eric: "Eric"},
        scope_id="relationship_context:read",
        manifest=rel_manifest,
        disclosure_tier="owner_raw",
        query_text="Who works on Topos with me?",
    )
    assert not any(str(i.get("edge_type") or "") in discourse_types for i in rel_items)

    graph_manifest = resolve_scope_manifest("graph:read")
    graph_items = graph_neighborhood_items(
        conn,
        anchor_ids=[white_house, eric],
        anchor_names={white_house: "White House", eric: "Eric"},
        scope_id="graph:read",
        manifest=graph_manifest,
        disclosure_tier="owner_raw",
        query_text="What is connected to the White House?",
    )
    assert any(str(i.get("edge_type") or "") in discourse_types for i in graph_items)
