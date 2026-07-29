"""E1/E2/E3/E4 sprint coverage (PLAN_TIMELINE_UNIFIED.md §9)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.features.test_complexity import _conn, _seed_live_shaped
from topos.features.timeline_rollup import timeline_daily_rollup
from topos.features.complexity.topics_daily import topics_daily
from topos.features.complexity.engine import get_complexity_summary, load_readings_history


def test_timeline_daily_rollup_buckets_lanes_and_births() -> None:
    conn = _conn()
    _seed_live_shaped(conn)
    result = timeline_daily_rollup(conn, days=90)
    assert len(result["days"]) == 90
    assert result["days"][0]["day"] == result["start"]
    total_lane_records = sum(sum(d["lanes"].values()) for d in result["days"])
    assert total_lane_records > 0
    total_entity_births = sum(d["births"]["entities"] for d in result["days"])
    assert total_entity_births > 0
    # tables absent from the seed (episodes) degrade to zeros, not errors
    assert all(isinstance(d["episodes"], int) for d in result["days"])


def test_timeline_daily_rollup_filters_junk_dates() -> None:
    conn = _conn()
    _seed_live_shaped(conn)
    conn.execute(
        "INSERT OR IGNORE INTO timeline (event_at, record_id, canonical_table) VALUES ('1970-01-01T00:00:00Z', 'junk1', 'conversation_messages')"
    )
    result = timeline_daily_rollup(conn, days=90)
    assert all(d["day"] > "2000-01-01" for d in result["days"])


def test_topics_daily_shares_sum_to_one_and_carry_labels() -> None:
    conn = _conn()
    _seed_live_shaped(conn)
    result = topics_daily(conn, days=90, top=5)
    assert len(result["days"]) == 90
    busy = [d for d in result["days"] if d["total"] > 0]
    assert busy, "seed data should produce clustered evidence days"
    for day in busy:
        assert abs(sum(day["shares"].values()) - 1.0) < 0.02
    assert result["topics"], "topic index should not be empty"
    for topic in result["topics"]:
        assert topic["label"]
        assert topic["first_day"] <= topic["last_day"]


def test_summary_carries_readings_history_with_all_scores() -> None:
    conn = _conn()
    _seed_live_shaped(conn)
    summary = get_complexity_summary(conn, recompute=True)
    history = summary.get("readings_history")
    assert isinstance(history, list) and history, "recompute writes today's snapshot"
    latest = history[-1]
    for key in ("focus", "clarity", "breadth", "pipeline"):
        assert latest.get(key) is not None, key
    assert load_readings_history(conn)[-1]["day"] == latest["day"]


def test_badges_and_intents_expose_instants() -> None:
    import json

    from topos.features.triage.badges import earned_badges

    conn = _conn()
    _seed_live_shaped(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_objects (
            object_id TEXT PRIMARY KEY, signal_dimension TEXT, object_type TEXT,
            object_key TEXT, payload_json TEXT, confidence REAL, source_refs_json TEXT,
            valid_from TEXT NOT NULL, valid_to TEXT, extractor_version TEXT,
            created_at TEXT, updated_at TEXT, created_by TEXT
        )
        """
    )
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, valid_from) "
        "VALUES ('b1', 'intentions', 'badge', 'badge:steered', ?, ?)",
        (json.dumps({"badge_id": "steered", "label": "Steered"}), (now - timedelta(days=3)).isoformat()),
    )
    badges = earned_badges(conn)
    assert badges and badges[0].get("earned_at"), "award instant must ride the payload"
