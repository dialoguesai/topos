"""Breaks found by the adversarial retest, kept as permanent regression tests.

Each test names a confirmed break from the 2026-08-26 attack pass. The scratch attack files
that found them are evidence; these are the durable enforcement.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_directed import (
    MAX_BROADCAST_ROSTER,
    SELF_KEY,
    _parse_ts,
    build_dyad_stats,
    extract_directed_dyadic_edges,
)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "adv.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    yield c
    c.close()


def _msg(conn, conv, mid, sender, when, is_self=0):
    conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                 (conv, mid, DS, sender, when, is_self, "imessage", None))


def test_a_peer_named_self_does_not_merge_into_the_owner(conn):
    """The literal token 'self' exists as a sender_id on the live corpus. A non-self message
    carrying it used to mint a self->self edge and an owner-owner dyad."""
    _msg(conn, "c1", "m1", "self", T0.isoformat(), is_self=0)
    _msg(conn, "c1", "m2", None, (T0 + timedelta(minutes=5)).isoformat(), is_self=1)
    conn.commit()
    acc = extract_directed_dyadic_edges(conn, DS)
    assert all(not (k[3] == SELF_KEY and k[4] == SELF_KEY) for k in acc), \
        "an unattributable sender must be skipped, never merged with the owner"
    stats = build_dyad_stats(conn, DS)
    assert all(not (r[1] == SELF_KEY and r[2] == SELF_KEY) for r in stats)


def test_one_naive_timestamp_does_not_zero_the_lane(conn):
    """One tz-less event_at used to raise TypeError and take the whole directed lane down.
    Naive is stamped UTC — the storage convention — because dropping the row would
    un-count a real message and guessing a zone would invent a time."""
    _msg(conn, "c1", "m1", "peer", "2026-05-01T09:00:00+00:00")
    _msg(conn, "c1", "m2", "peer", "2026-05-01 10:00:00")   # naive
    conn.commit()
    acc = extract_directed_dyadic_edges(conn, DS)
    assert sum(v.msgs for v in acc.values()) == 2
    assert build_dyad_stats(conn, DS), "the rollup survives too"


def test_timezone_offsets_order_by_instant_not_by_text(conn):
    """TEXT ORDER BY put '09:00+02:00' after '08:30+00:00' although it happened first —
    producing negative reply latency and crediting initiation to the responder."""
    _msg(conn, "c1", "m1", "peer", "2026-05-01T09:00:00+02:00")   # 07:00Z — actually first
    _msg(conn, "c1", "m2", None, "2026-05-01T08:30:00+00:00", is_self=1)  # 08:30Z — a reply
    conn.commit()
    acc = extract_directed_dyadic_edges(conn, DS)
    out = acc[("2026-05", "imessage", "dm", SELF_KEY, "peer")]
    inb = acc[("2026-05", "imessage", "dm", "peer", SELF_KEY)]
    assert inb.sessions_initiated == 1 and out.sessions_initiated == 0, \
        "the peer spoke first in real time"
    assert all(l >= 0 for l in out.latencies), "latency can never be negative"


def test_a_future_message_poisons_only_its_own_dyad(conn):
    """ref used to be the corpus MAX timestamp, so one future-dated row marked every other
    dyad dormant. ref is now wall-time and gaps clamp at zero."""
    for i in range(10):
        _msg(conn, "c1", f"a{i}", "peer_ok", (T0 + timedelta(days=i)).isoformat())
        _msg(conn, "c1", f"b{i}", None, (T0 + timedelta(days=i, minutes=3)).isoformat(), is_self=1)
    _msg(conn, "c2", "f1", "peer_future", "2099-01-01T00:00:00+00:00")
    conn.commit()
    ref = T0 + timedelta(days=10)
    rows = build_dyad_stats(conn, DS, now=ref)
    by_peer = {(r[1] if r[1] != SELF_KEY else r[2]): r for r in rows}
    COLS_RECENT_GAP = 19
    ok = by_peer["peer_ok"]
    assert ok[COLS_RECENT_GAP] is not None and ok[COLS_RECENT_GAP] <= 1.0, \
        "the healthy dyad's recency must not be dragged by someone else's bad clock"
    fut = by_peer["peer_future"]
    assert fut[COLS_RECENT_GAP] == 0.0, "a future gap clamps at zero, never negative"


def test_drift_is_bounded(conn):
    """Measured 58x explosion when ref preceded the last message. Bounded [0, 10]."""
    for i in range(10):
        _msg(conn, "c1", f"a{i}", "peer", (T0 + timedelta(days=i)).isoformat())
        _msg(conn, "c1", f"b{i}", None, (T0 + timedelta(days=i, minutes=3)).isoformat(), is_self=1)
    conn.commit()
    rows = build_dyad_stats(conn, DS, now=T0 - timedelta(days=5))
    DRIFT = 20
    for r in rows:
        if r[DRIFT] is not None:
            assert 0.0 <= r[DRIFT] <= 10.0


def test_a_giant_room_mints_no_broadcast_rows(conn):
    """Fan-out is quadratic in the roster: a 500-speaker room minted 250k rows per period.
    A room past the cap is an announcement channel, and minting nothing is the honest
    reading of it."""
    for i in range(MAX_BROADCAST_ROSTER + 5):
        _msg(conn, "g1", f"m{i}", f"speaker_{i}", (T0 + timedelta(minutes=i)).isoformat())
    conn.commit()
    acc = extract_directed_dyadic_edges(conn, DS)
    assert not [k for k in acc if k[2] == "group_broadcast"]


def test_epoch_seconds_parse_instead_of_vanishing():
    """Digit-string timestamps exist on the live copy and used to contribute nothing."""
    dt = _parse_ts("1777777777")
    assert dt is not None and dt.tzinfo is not None
    ms = _parse_ts("1777777777000")
    assert ms == dt


def test_unknown_recency_is_never_warm():
    """NULL recent_gap coerced to 0.0 — 'we don't know when you last spoke' scored as 'you
    spoke moments ago'. Unknown caps at steady."""
    from topos.features.derivation.social_kernels import compute_warmth

    dyads = []
    for i in range(4):
        dyads.append({"dataset_id": "d", "a_key": "self", "b_key": f"p{i}",
                      "total_msgs": 500, "a_to_b": 250, "b_to_a": 250, "balance": 0.0,
                      "reciprocal_periods": 3, "active_periods": 3,
                      "reciprocal_streak_weeks": 3,
                      "recent_gap_days": None if i == 0 else 1.0,
                      "drift_ratio": 1.0, "median_gap_days": 1.0, "tie_state": "active"})
    out = {r["peer_key"]: r["warmth_band"] for r in compute_warmth(dyads)}
    assert out["p0"] == "steady" and out["p1"] == "warm"


def test_one_dyad_cannot_calibrate_against_itself():
    """A quantile of a distribution of one is that one value — the single dyad banded
    'warm' by tautology."""
    from topos.features.derivation.social_kernels import compute_warmth

    d = {"dataset_id": "d", "a_key": "self", "b_key": "p", "total_msgs": 100,
         "a_to_b": 50, "b_to_a": 50, "balance": 0.0, "reciprocal_periods": 3,
         "active_periods": 3, "reciprocal_streak_weeks": 3, "recent_gap_days": 0.5,
         "drift_ratio": 1.0, "median_gap_days": 1.0, "tie_state": "active"}
    out = compute_warmth([d])
    assert out[0]["warmth_band"] == "steady"
    assert out[0]["threshold_basis"]["calibration"] == "insufficient_dyads"


def test_a_kernel_returning_none_abstains():
    """None was coerced to [] — abstention was unreachable from inside a kernel."""
    from topos.features.derivation.kernels import register_kernel, run_lens
    from topos.features.derivation.packs import Lens

    @register_kernel("t_returns_none")
    def _k(conn, pack, lens, owner):
        return None

    class P:
        pack = "t.p"

    res = run_lens(sqlite3.connect(":memory:"), P(), Lens(kind="t_returns_none"), "ent_o")
    assert res.abstained and res.reason == "kernel_returned_none"
