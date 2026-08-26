"""L1-5/6 — persistence, and the lifetime rollup.

The rollup is where L1 stops counting and starts making claims about relationships, so the
tests here are mostly about the claims being the ones we mean.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_directed import (
    MESSENGER_DIRECTED_EDGES_TABLE,
    MESSENGER_DYAD_STATS_TABLE,
    SELF_KEY,
    TIE_BROADCAST_ONLY,
    build_dyad_stats,
    create_directed_tables,
    extract_directed_dyadic_edges,
    persist_directed_edges,
    persist_dyad_stats,
    rows_for_persist,
)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"

COLS = ["dataset_id", "a", "b", "involves_self", "peer_class", "total", "a_to_b", "b_to_a",
        "balance", "first", "last", "active_periods", "recip_periods", "streak_m",
        "streak_w", "recip_m", "recip_w", "max_gap", "med_gap", "recent_gap", "drift", "tie"]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "m.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    create_directed_tables(c)
    yield c
    c.close()


def _msg(conn, conv, mid, sender, minutes, is_self=0, src="imessage"):
    conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                 (conv, mid, DS, sender, (T0 + timedelta(minutes=minutes)).isoformat(),
                  is_self, src, None))


def _stats(conn, **kw):
    return [dict(zip(COLS, r)) for r in build_dyad_stats(conn, DS, **kw)]


# --- persistence ---

def test_edges_round_trip(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 5, is_self=1)
    conn.commit()
    rows = rows_for_persist(extract_directed_dyadic_edges(conn, DS), DS, 21600)
    assert persist_directed_edges(conn, DS, rows) == len(rows)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE}").fetchone()[0] == len(rows)


def test_persisting_twice_does_not_duplicate(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    conn.commit()
    rows = rows_for_persist(extract_directed_dyadic_edges(conn, DS), DS, 21600)
    persist_directed_edges(conn, DS, rows)
    persist_directed_edges(conn, DS, rows)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE}").fetchone()[0] == len(rows)


def test_pruning_is_scoped_to_the_periods_recomputed(conn):
    """A partial pass — one connector, one month — must not erase everything else.

    A blanket DELETE would make any narrow recompute silently destructive, which is the
    failure mode that turns a cheap incremental job into a data-loss event.
    """
    conn.execute(
        f"""INSERT INTO {MESSENGER_DIRECTED_EDGES_TABLE}
            (dataset_id, period_key, connector, edge_kind, from_key, to_key, msgs,
             sessions_initiated, replies, session_gap_seconds, created_at, updated_at)
            VALUES (?,'2026-01','imessage','dm','x','y',5,1,0,21600,'t','t')""", (DS,))
    conn.commit()
    _msg(conn, "c1", "m1", "peer", 0)
    conn.commit()
    rows = rows_for_persist(extract_directed_dyadic_edges(conn, DS), DS, 21600)
    persist_directed_edges(conn, DS, rows)
    kept = conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE} WHERE period_key='2026-01'"
    ).fetchone()[0]
    assert kept == 1, "an untouched period must survive a partial recompute"


def test_dyad_stats_round_trip(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 5, is_self=1)
    conn.commit()
    rows = build_dyad_stats(conn, DS)
    assert persist_dyad_stats(conn, DS, rows) == len(rows)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DYAD_STATS_TABLE}").fetchone()[0] == len(rows)


# --- the sign bug that measurement caught ---

@pytest.mark.parametrize("peer", ["+15125551234", "zoe@example.com"])
def test_balance_sign_does_not_depend_on_how_the_peer_sorts(conn, peer):
    """The regression that live data surfaced.

    `dyad_key` sorts canonically, so a phone peer ('+1…' < 's') lands in a_key while an email
    peer ('zoe@…' > 's') lands in b_key. Defining balance as (a_to_b - b_to_a) therefore
    flips sign on the counterparty's phone number — and "who is one-sided" would invert for
    no reason at all. Positive must always mean THE OWNER sends more.
    """
    for i in range(8):
        _msg(conn, "c1", f"o{i}", None, i, is_self=1)   # owner: 8
    for i in range(2):
        _msg(conn, "c1", f"p{i}", peer, 20 + i)          # peer: 2
    conn.commit()
    d = _stats(conn)[0]
    assert d["balance"] > 0, "owner sent 8 of 10 — balance must be positive either way"
    assert abs(d["balance"] - 0.6) < 1e-9


# --- streaks: contact vs reciprocal ---

def test_a_broadcast_only_tie_has_contact_but_no_reciprocity(conn):
    """Catalog #19 in one row: someone who only ever talks AT you."""
    for i in range(6):
        _msg(conn, "c1", f"p{i}", "peer", i * 60 * 24 * 7)   # weekly, one direction
    conn.commit()
    d = _stats(conn)[0]
    assert d["streak_w"] >= 5
    assert d["recip_w"] == 0
    assert d["recip_periods"] == 0
    assert d["tie"] == TIE_BROADCAST_ONLY


def test_reciprocal_streak_needs_both_directions_in_the_bucket(conn):
    _msg(conn, "c1", "a1", "peer", 0)
    _msg(conn, "c1", "a2", None, 30, is_self=1)              # week 1: both
    _msg(conn, "c1", "b1", "peer", 7 * 24 * 60)              # week 2: peer only
    conn.commit()
    d = _stats(conn)[0]
    assert d["streak_w"] == 2
    assert d["recip_w"] == 1, "only one week had traffic both ways"


def test_a_streak_cannot_exceed_the_corpus_calendar(conn):
    """Indexing runs off the dyad's own first bucket would let a dyad that starts late claim
    a streak longer than the corpus is old."""
    _msg(conn, "c1", "m1", "peer", 0)
    _msg(conn, "c1", "m2", None, 1, is_self=1)
    conn.commit()
    d = _stats(conn)[0]
    assert d["streak_m"] == 1 and d["streak_w"] == 1


# --- drift is measured against the dyad's OWN baseline ---

def test_drift_compares_a_dyad_to_itself_not_to_other_dyads(conn):
    """A monthly correspondent is not drifting because a daily one exists."""
    for i in range(20):
        _msg(conn, "c1", f"p{i}", "peer", i * 60 * 24)       # daily for 20 days
        _msg(conn, "c1", f"o{i}", None, i * 60 * 24 + 30, is_self=1)
    conn.commit()
    ref = T0 + timedelta(days=19)
    d = _stats(conn, now=ref)[0]
    assert d["drift"] is not None and d["drift"] > 0.8, "a steady dyad is not drifting"


def test_a_dyad_that_stopped_reads_as_drifted(conn):
    for i in range(10):
        _msg(conn, "c1", f"p{i}", "peer", i * 60 * 24)
        _msg(conn, "c1", f"o{i}", None, i * 60 * 24 + 30, is_self=1)
    conn.commit()
    ref = T0 + timedelta(days=200)
    d = _stats(conn, now=ref)[0]
    assert d["drift"] == 0.0
    assert d["recent_gap"] > 180
    assert d["tie"] == "dormant"


def test_peer_class_rides_on_the_dyad(conn):
    _msg(conn, "c1", "m1", "262966", 0)
    _msg(conn, "c1", "m2", None, 5, is_self=1)
    conn.commit()
    assert _stats(conn)[0]["peer_class"] == "automated"


def test_involves_self_is_set_for_owner_dyads(conn):
    _msg(conn, "c1", "m1", "peer", 0)
    conn.commit()
    d = _stats(conn)[0]
    assert d["involves_self"] == 1
    assert SELF_KEY in (d["a"], d["b"])
