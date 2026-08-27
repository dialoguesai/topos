"""L1-2/3/4 — direction, sessions, initiations and reply latency from one pass.

Sessions, initiations and replies are three readings of the same fact: where the silences
are. So they are tested together, on corpora small enough that the right answer is arithmetic
rather than opinion.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_directed import (
    DEFAULT_SESSION_GAP_SECONDS,
    EDGE_KIND_DM,
    EDGE_KIND_GROUP_BROADCAST,
    EDGE_KIND_GROUP_REPLY,
    SELF_KEY,
    extract_directed_dyadic_edges,
)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "m.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    yield c
    c.close()


def _msg(conn, conv, mid, sender, minutes, is_self=0, src="imessage", reply_to=None):
    conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                 (conv, mid, DS, sender, (T0 + timedelta(minutes=minutes)).isoformat(),
                  is_self, src, reply_to))


def _edges(conn, **kw):
    return extract_directed_dyadic_edges(conn, DS, **kw)


# --- direction ---

def test_a_dm_produces_two_rows_that_sum_to_the_raw_count(conn):
    """A1, in miniature: the falsifiable core of the whole layer."""
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 5, is_self=1)
    _msg(conn, "c1", "m3", "peer", 10)
    conn.commit()
    acc = _edges(conn)
    out = acc[("2026-05", "imessage", EDGE_KIND_DM, SELF_KEY, "peer")]
    inb = acc[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)]
    assert out.msgs == 1 and inb.msgs == 2
    assert out.msgs + inb.msgs == 3, "directed rows must conserve the raw message count"


def test_direction_survives_the_owner_speaking_first(conn):
    _msg(conn, "c1", "m1", None, 0, is_self=1)
    conn.commit()
    acc = _edges(conn)
    assert ("2026-05", "imessage", EDGE_KIND_DM, SELF_KEY, "peer") not in acc, \
        "with no peer message the counterparty is unknown — inventing one would be a guess"


# --- sessions and initiations ---

def test_a_long_silence_opens_a_new_session(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", "peer", 5)                       # same session
    _msg(conn, "c1", "m3", "peer", 5 + 7 * 60)              # 7h later: new session
    conn.commit()
    e = _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)]
    assert e.sessions_initiated == 2
    assert e.msgs == 3


def test_consecutive_messages_from_one_person_are_one_turn(conn):
    """Otherwise 'who initiates' silently becomes 'who is chattiest'."""
    for i in range(6):
        _msg(conn, "c1", f"m{i}", "peer", i)
    conn.commit()
    e = _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)]
    assert e.sessions_initiated == 1
    assert e.replies == 0


def test_initiations_split_across_both_directions(conn):
    """A3: within a dyad-period, the two directions' initiations sum to the session count."""
    _msg(conn, "c1", "m1", "peer", 0)                       # peer opens
    _msg(conn, "c1", "m2", None, 2, is_self=1)
    _msg(conn, "c1", "m3", None, 2 + 8 * 60, is_self=1)     # owner opens after 8h
    conn.commit()
    acc = _edges(conn)
    out = acc[("2026-05", "imessage", EDGE_KIND_DM, SELF_KEY, "peer")]
    inb = acc[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)]
    assert inb.sessions_initiated == 1 and out.sessions_initiated == 1


def test_the_threshold_actually_moves_the_answer(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", "peer", 3 * 60)                  # 3h gap
    conn.commit()
    k = ("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)
    assert _edges(conn)[k].sessions_initiated == 1                       # 6h default
    assert _edges(conn, session_gap_seconds=3600)[k].sessions_initiated == 2


# --- replies and latency ---

def test_a_reply_is_a_change_of_speaker_inside_a_session(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 4, is_self=1)              # reply after 4 min
    conn.commit()
    out = _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, SELF_KEY, "peer")]
    assert out.replies == 1
    assert out.latencies == [240.0]


def test_the_first_message_after_a_silence_is_not_a_reply(conn):
    """It opens a session. Counting it as a reply would report a latency of 'however long
    they ignored you', which is a different fact wearing the same name."""
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 9 * 60, is_self=1)
    conn.commit()
    out = _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, SELF_KEY, "peer")]
    assert out.replies == 0 and out.latencies == []
    assert out.sessions_initiated == 1


def test_median_latency_is_the_median_not_the_mean(conn):
    """One three-day reply must not drag the number it sits in."""
    from topos.analytics.messenger_directed import _median

    assert _median([60.0, 120.0, 259200.0]) == 120.0


# --- periods and connectors ---

def test_edges_split_by_period(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", "peer", 45 * 24 * 60)
    conn.commit()
    acc = _edges(conn)
    assert {k[0] for k in acc} == {"2026-05", "2026-06"}


def test_edges_split_by_connector(conn):
    _msg(conn, "c1", "m1", "peer", 0, src="imessage")
    _msg(conn, "c2", "m2", "peer", 1, src="signal")
    conn.commit()
    assert {k[1] for k in _edges(conn)} == {"imessage", "signal"}


def test_a_connector_filter_narrows_the_pass(conn):
    _msg(conn, "c1", "m1", "peer", 0, src="imessage")
    _msg(conn, "c2", "m2", "peer", 1, src="signal")
    conn.commit()
    assert {k[1] for k in _edges(conn, connector="imessage")} == {"imessage"}


# --- groups ---

def test_a_group_broadcast_never_lands_in_the_dm_lane(conn):
    _msg(conn, "g1", "m1", "a", 0)
    _msg(conn, "g1", "m2", "b", 1)
    _msg(conn, "g1", "m3", None, 2, is_self=1)
    conn.commit()
    assert not [k for k in _edges(conn) if k[2] == EDGE_KIND_DM]


def test_a_reply_link_in_a_group_is_a_hard_directed_edge(conn):
    _msg(conn, "g1", "m1", "a", 0)
    _msg(conn, "g1", "m2", "b", 1)
    _msg(conn, "g1", "m3", None, 2, is_self=1, reply_to="m1")
    conn.commit()
    acc = _edges(conn)
    assert acc[("2026-05", "imessage", EDGE_KIND_GROUP_REPLY, SELF_KEY, "a")].msgs == 1


def test_broadcast_fans_out_to_the_room_but_stays_in_its_own_kind(conn):
    _msg(conn, "g1", "m1", "a", 0)
    _msg(conn, "g1", "m2", "b", 1)
    _msg(conn, "g1", "m3", None, 2, is_self=1)
    conn.commit()
    b = {k: v for k, v in _edges(conn).items() if k[2] == EDGE_KIND_GROUP_BROADCAST}
    # owner's one message reaches both speakers
    assert b[("2026-05", "imessage", EDGE_KIND_GROUP_BROADCAST, SELF_KEY, "a")].msgs == 1
    assert b[("2026-05", "imessage", EDGE_KIND_GROUP_BROADCAST, SELF_KEY, "b")].msgs == 1


# --- hygiene ---

def test_a_message_without_a_timestamp_is_dropped_not_defaulted(conn):
    """Defaulting to now would invent a conversation that happened at derivation time — the
    exact `created_at`-for-`event_at` trap L5's acceptance criteria call out."""
    _msg(conn, "c1", "m1", "peer", 0)
    conn.execute("INSERT INTO conversation_messages VALUES ('c1','m9',?,'peer',NULL,0,'imessage',NULL)", (DS,))
    conn.commit()
    assert _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)].msgs == 1


def test_the_walk_resets_between_conversations(conn):
    """Otherwise the last message of one conversation sets the session state of the next."""
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c2", "m2", "other", 1)
    conn.commit()
    acc = _edges(conn)
    assert acc[("2026-05", "imessage", EDGE_KIND_DM, "other", SELF_KEY)].sessions_initiated == 1


def test_first_and_last_timestamps_bound_the_period(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", "peer", 30)
    conn.commit()
    e = _edges(conn)[("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)]
    assert e.first_ts < e.last_ts
    assert DEFAULT_SESSION_GAP_SECONDS == 21600


# --- G6: topic mix on the edge ---

def test_topics_land_on_the_edge_with_coverage(conn):
    """Same contract as affect: counts plus the coverage that keeps the mix honest — a mix
    over three labelled messages must not impersonate one over three hundred."""
    import json as _json

    from topos.analytics.messenger_directed import attach_topics

    conn.execute("""CREATE TABLE message_topics (topic_id TEXT PRIMARY KEY, record_id TEXT,
        message_id TEXT, topic TEXT)""")
    for i in range(4):
        _msg(conn, "c1", f"m{i}", "peer", i * 30)
    conn.execute("INSERT INTO message_topics VALUES ('t1','m0','m0','hardware build')")
    conn.execute("INSERT INTO message_topics VALUES ('t2','m1','m1','hardware build')")
    conn.commit()
    acc = _edges(conn)
    topics = attach_topics(conn, DS, acc)
    key = ("2026-05", "imessage", EDGE_KIND_DM, "peer", SELF_KEY)
    assert _json.loads(topics[key]["topic_counts_json"]) == {"hardware build": 2}
    assert topics[key]["topic_coverage"] == 0.5, "2 of 4 messages labelled — said, not hidden"


def test_a_node_without_topic_enrichment_attaches_nothing(conn):
    """iMessage ships un-enrolled in the topics job on purpose — an LLM generation per
    message per sync is a cost the source registry declines. Zero enrichment must mean
    zero topic fields, never empty-but-present ones."""
    from topos.analytics.messenger_directed import attach_topics

    _msg(conn, "c1", "m1", "peer", 0)
    conn.commit()
    assert attach_topics(conn, DS, _edges(conn)) == {}
