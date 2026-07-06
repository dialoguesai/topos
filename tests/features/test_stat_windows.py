"""Stat windows: daily bucketing, windowed reads, windowed insight variants.

The audit found every stat definition shipped with windows=["all"] and no
daily buckets, so "typically" always meant "averaged over your whole life".
These tests pin the window path end to end: seeds carry windows, folds write
daily buckets, read_state(window=...) excludes old mass, and insights emit a
"last 30 days" variant with its own stable fact id.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.features.stats import definitions as defs
from topos.features.stats.engine import StatsEngine


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE stat_definitions (
            stat_id TEXT PRIMARY KEY, canonical_table TEXT, group_by TEXT,
            stat_kind TEXT, value_expr TEXT, dimension TEXT,
            decay_half_life_days REAL, windows_json TEXT, enabled INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE stat_state (
            stat_id TEXT, group_key TEXT, bucket_date TEXT, state_json TEXT,
            watermark TEXT, updated_at TEXT,
            PRIMARY KEY (stat_id, group_key, bucket_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE stat_seen (
            stat_id TEXT, record_id TEXT, seen_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (stat_id, record_id)
        )
        """
    )
    yield conn
    conn.close()


def test_all_seeds_configure_windows(conn):
    defs.seed_definitions(conn)
    rows = conn.execute("SELECT stat_id, windows_json FROM stat_definitions").fetchall()
    assert rows
    for stat_id, windows_json in rows:
        windows = json.loads(windows_json)
        assert "all" in windows, stat_id
        assert any(w.startswith("d") for w in windows), (
            f"{stat_id} has no rolling window — audit regression"
        )


def test_reseed_updates_existing_rows(conn):
    # Simulate the live node: definitions seeded before windows existed.
    conn.execute(
        """
        INSERT INTO stat_definitions (stat_id, canonical_table, group_by,
            stat_kind, value_expr, dimension, decay_half_life_days,
            windows_json, enabled)
        VALUES ('messages.volume.by_contact', 'conversation_messages',
                'contact_id', 'count', 'one', 'relationships', NULL,
                '["all"]', 1)
        """
    )
    conn.commit()
    defs.seed_definitions(conn)
    row = conn.execute(
        "SELECT windows_json FROM stat_definitions WHERE stat_id='messages.volume.by_contact'"
    ).fetchone()
    assert json.loads(row[0]) == ["all", "d7", "d30", "d90"]


def test_fold_writes_daily_buckets_and_window_reads_exclude_old(conn):
    defs.seed_definitions(conn)
    engine = StatsEngine(conn)
    fresh_ts = _now() - timedelta(days=2)
    old_ts = _now() - timedelta(days=100)
    rows = [
        {"record_id": "m1", "sender_id": "alice", "event_at": fresh_ts.isoformat()},
        {"record_id": "m2", "sender_id": "alice", "event_at": fresh_ts.isoformat()},
        {"record_id": "m3", "sender_id": "alice", "event_at": fresh_ts.isoformat()},
        {"record_id": "m4", "sender_id": "alice", "event_at": old_ts.isoformat()},
        {"record_id": "m5", "sender_id": "alice", "event_at": old_ts.isoformat()},
    ]
    engine.fold_batch(rows)

    buckets = conn.execute(
        "SELECT DISTINCT bucket_date FROM stat_state"
        " WHERE stat_id='messages.volume.by_contact' AND bucket_date != ''"
    ).fetchall()
    assert len(buckets) == 2  # one daily bucket per distinct day

    all_state = engine.read_state("messages.volume.by_contact", group_key="alice")
    assert int(all_state.get("n")) == 5

    recent = engine.read_state(
        "messages.volume.by_contact", group_key="alice", window="d30"
    )
    assert int(recent.get("n") or 0) == 3  # the two 100-day-old rows excluded


def test_insights_emit_windowed_variant(conn):
    defs.seed_definitions(conn)
    engine = StatsEngine(conn)
    fresh_ts = _now() - timedelta(days=2)
    rows = [
        {"record_id": f"m{i}", "sender_id": "alice", "event_at": fresh_ts.isoformat()}
        for i in range(4)
    ]
    engine.fold_batch(rows)

    from topos.features.stats.insights import render_insights

    insights = render_insights(engine)
    volume = [
        i
        for i in insights
        if i["stat_id"] == "messages.volume.by_contact" and i["group_key"] == "alice"
    ]
    windows = {i.get("window") for i in volume}
    assert None in windows or "" in windows.union({None})  # all-time variant
    windowed = [i for i in volume if i.get("window")]
    assert windowed, "no windowed insight emitted"
    assert windowed[0]["window"] in ("d7", "d30")
    assert "last" in windowed[0]["text"].lower()


def test_windowed_fact_ids_are_distinct(conn):
    defs.seed_definitions(conn)
    engine = StatsEngine(conn)
    fresh_ts = _now() - timedelta(days=2)
    rows = [
        {"record_id": f"m{i}", "sender_id": "alice", "event_at": fresh_ts.isoformat()}
        for i in range(4)
    ]
    engine.fold_batch(rows)

    written_facts = []

    class _Signal:
        def put_fact(self, fact):
            written_facts.append(fact)

    class _Adapters:
        signal = _Signal()

    engine.promote_insights(_Adapters())
    ids = [f["fact_id"] for f in written_facts]
    assert len(ids) == len(set(ids)), "fact ids must be unique"
    base = "stat:messages.volume.by_contact:alice"
    assert base in ids
    assert any(i.startswith(base + ":d") for i in ids), "windowed fact id missing"
