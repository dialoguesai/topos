"""Full-population counts behind the type-specific entity cards.

The cards state readings — "something you read about", "somewhere you go",
"worked on, never written about" — and every one of them is a claim about
PROPORTION between sources. ``recent_mentions`` is a twenty-row sample, so a
card built on it would be asserting a measurement nobody made. These tests
pin the two properties that make the aggregates safe to build a sentence on:
they cover the whole set, and they respect the black-hole guard.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.reads import entity_card_aggregates, get_entity_detail
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.storage.db.migrations import apply_all_migrations


def _entity(c, eid, etype, name):
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
        " VALUES (?,?,?,?)",
        (eid, etype, name, name.lower()),
    )


def _mention(c, eid, record_id, table, authored=0):
    c.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table,"
        " surface_text, authored_by_owner) VALUES (?,?,?,?,?,?)",
        (f"m-{eid}-{record_id}-{table}", eid, record_id, table, "x", authored),
    )


def _edge(c, src, dst, etype):
    c.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight)"
        " VALUES (?,?,?,?,1.0)",
        (f"e-{src}-{dst}-{etype}", src, dst, etype),
    )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "card_aggregates.db"))
    apply_all_migrations(c)
    _entity(c, "ent-place", "place", "Harbour Cafe")
    _entity(c, "ent-friend", "person", "Sam Okoye")
    _entity(c, "ent-secret", "person", "Dana Reyes")
    _entity(c, "ent-goal", "goal", "Ship the thing")
    # 3 visits, 2 journal entries the owner wrote, 1 message someone else sent.
    for i in range(3):
        _mention(c, "ent-place", f"loc-{i}", "location_events", authored=0)
    for i in range(2):
        _mention(c, "ent-place", f"jrn-{i}", "journal_entries", authored=1)
    _mention(c, "ent-place", "msg-0", "conversation_messages", authored=0)
    _edge(c, "ent-place", "ent-friend", "co_occurrence")
    _edge(c, "ent-place", "ent-secret", "co_occurrence")
    _edge(c, "ent-goal", "ent-place", "relates_to")
    c.commit()
    return c


def _owner(conn):
    return BlackholeGuard(conn, caller_class=CallerClass.OWNER_UI)


def _stranger(conn):
    return BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)


def test_mention_sources_count_every_mention_not_a_sample(conn):
    agg = entity_card_aggregates(conn, "ent-place", guard=_owner(conn))
    by_table = {r["table"]: r for r in agg["mention_sources"]}
    assert by_table["location_events"]["count"] == 3
    assert by_table["journal_entries"]["count"] == 2
    assert by_table["conversation_messages"]["count"] == 1
    # Descending by count, so the card's leading source needs no re-sort.
    assert [r["table"] for r in agg["mention_sources"]][0] == "location_events"


def test_owner_authored_share_is_per_table(conn):
    """The org card's whole judgement is "did YOU write this, or did it appear".

    A share computed across all tables at once would let 455 owner-authored
    journal lines drown a source that is entirely someone else's.
    """
    agg = entity_card_aggregates(conn, "ent-place", guard=_owner(conn))
    by_table = {r["table"]: r for r in agg["mention_sources"]}
    assert by_table["journal_entries"]["owner_authored"] == 2
    assert by_table["location_events"]["owner_authored"] == 0
    assert by_table["conversation_messages"]["owner_authored"] == 0


def test_mention_sources_reconcile_with_the_total(conn):
    """Sum of the parts is the whole — the property that lets a card show a %.

    An unattributed mention is bucketed under "", never dropped, so a percentage
    computed against this denominator cannot exceed 100.
    """
    _mention(conn, "ent-place", "orphan-0", None)
    conn.commit()
    agg = entity_card_aggregates(conn, "ent-place", guard=_owner(conn))
    total = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id='ent-place'"
    ).fetchone()[0]
    assert sum(r["count"] for r in agg["mention_sources"]) == total
    assert any(r["table"] == "" for r in agg["mention_sources"])


def test_neighbor_counts_keep_direction(conn):
    """`participates_in` (they were there) vs `mentions` (they were named).

    Collapsing direction is how a card ends up saying someone who was discussed
    was in the room.
    """
    agg = entity_card_aggregates(conn, "ent-place", guard=_owner(conn))
    counts = {(r["edge_type"], r["direction"], r["entity_type"]): r["count"]
              for r in agg["neighbor_counts"]}
    assert counts[("co_occurrence", "out", "person")] == 2
    assert counts[("relates_to", "in", "goal")] == 1


def test_neighbor_counts_are_not_capped_like_the_connection_list(conn):
    """`connections` is capped at 12 by weight; a COUNT must not be."""
    for i in range(30):
        _entity(conn, f"ent-bulk-{i}", "person", f"Bulk Person {i}")
        _edge(conn, "ent-place", f"ent-bulk-{i}", "co_occurrence")
    conn.commit()
    agg = entity_card_aggregates(conn, "ent-place", guard=_owner(conn))
    counts = {(r["edge_type"], r["direction"], r["entity_type"]): r["count"]
              for r in agg["neighbor_counts"]}
    assert counts[("co_occurrence", "out", "person")] == 32

    detail = get_entity_detail(conn, "ent-place", guard=_owner(conn))
    assert len(detail["connections"]) <= 12  # the LIST still pages
    assert detail["neighbor_counts"] == agg["neighbor_counts"]


def test_a_black_holed_neighbour_is_not_counted(conn):
    """A count that includes a hidden neighbour leaks that it exists.

    The neighbour list already filters; a tally beside it that did not would
    let a reader subtract one from the other.
    """
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-secret")
    conn.commit()
    counts = {
        (r["edge_type"], r["direction"], r["entity_type"]): r["count"]
        for r in entity_card_aggregates(conn, "ent-place", guard=_stranger(conn))["neighbor_counts"]
    }
    assert counts[("co_occurrence", "out", "person")] == 1
    # The owner still sees both.
    owner_counts = {
        (r["edge_type"], r["direction"], r["entity_type"]): r["count"]
        for r in entity_card_aggregates(conn, "ent-place", guard=_owner(conn))["neighbor_counts"]
    }
    assert owner_counts[("co_occurrence", "out", "person")] == 2


def test_mentions_in_a_withheld_record_are_not_counted(conn):
    """A record naming a protected entity is withheld whole.

    Counting its mentions of a VISIBLE entity would make the total disagree with
    the records the reader can open, and the size of that gap is itself a signal.
    """
    _mention(conn, "ent-secret", "jrn-0", "journal_entries", authored=1)
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-secret")
    conn.commit()
    stranger = {
        r["table"]: r["count"]
        for r in entity_card_aggregates(conn, "ent-place", guard=_stranger(conn))["mention_sources"]
    }
    assert stranger["journal_entries"] == 1  # jrn-0 withheld, jrn-1 kept
    owner = {
        r["table"]: r["count"]
        for r in entity_card_aggregates(conn, "ent-place", guard=_owner(conn))["mention_sources"]
    }
    assert owner["journal_entries"] == 2


def test_get_entity_detail_carries_the_aggregates(conn):
    detail = get_entity_detail(conn, "ent-place", guard=_owner(conn))
    assert "mention_sources" in detail and "neighbor_counts" in detail
    assert detail["mention_sources"][0]["table"] == "location_events"


def test_get_entity_detail_joins_topic_cluster_members(conn):
    cluster_id = "54bb73cf1ee14671"
    hub_id = f"topic_{cluster_id}"
    _entity(conn, hub_id, "topic", "Apps And Their Security")
    conn.execute(
        """
        INSERT INTO topic_clusters
            (cluster_id, label, dimension, member_count, source_mix_json,
             label_terms_json, model, metadata_json)
        VALUES (?, 'Apps And Their Security', 'memory', 2,
                '{"youtube_transcripts": 2}', '["apps","store"]', 'kmeans',
                '{"label_model":"llama3.2","term_label":"apps / store"}')
        """,
        (cluster_id,),
    )
    conn.execute(
        """
        INSERT INTO transcript_segments
            (segment_id, transcript_id, content, event_at, source_id)
        VALUES ('seg-apps-1', 'yt:NVZwqkxEX6g', 'the apps on the store',
                '2026-09-04T02:00:00Z', 'youtube_transcripts')
        """
    )
    conn.execute(
        """
        INSERT INTO topic_cluster_members
            (member_id, cluster_id, record_id, source_id, record_type, text_preview)
        VALUES ('m1', ?, 'seg-apps-1', 'youtube_transcripts', 'transcript_segment',
                'the apps on the store')
        """,
        (cluster_id,),
    )
    conn.commit()
    detail = get_entity_detail(conn, hub_id, guard=_owner(conn))
    assert detail is not None
    cluster = detail["cluster"]
    assert cluster["cluster_id"] == cluster_id
    assert cluster["member_count"] == 2
    assert cluster["label_terms"] == ["apps", "store"]
    assert cluster["label_model"] == "llama3.2"
    assert cluster["members"][0]["transcript_id"] == "yt:NVZwqkxEX6g"
    assert cluster["videos"][0]["id"] == "yt:NVZwqkxEX6g"
    assert detail["recent_mentions"][0]["surface_text"] == "the apps on the store"
    assert detail["mention_sources"][0]["table"] == "transcript_segments"
