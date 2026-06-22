"""Tests for fit composition layer."""

from __future__ import annotations

import sqlite3

from topos.features.fit.evaluator import compute_fit_readiness, evaluate_opportunity
from topos.features.signal.extraction.artifact_router import route_canonical_batch
from topos.features.signal.typed_stores.aggregates import recompute_all_gate_aggregates
from topos.features.signal.signal_object_store import SignalObjectStore
from topos.storage.db.migrations.extraction_artifacts import apply_extraction_artifacts_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _seed_harness(conn: sqlite3.Connection) -> None:
    apply_signal_objects_up(conn)
    apply_extraction_artifacts_up(conn)
    route_canonical_batch(
        conn,
        [
            {
                "canonical_table": "calendar_events",
                "event_id": "cal-006",
                "starts_at": "2026-03-16T11:00:00Z",
                "ends_at": "2026-03-16T13:00:00Z",
                "is_busy": False,
            },
            {
                "canonical_table": "calendar_events",
                "event_id": "cal-003",
                "starts_at": "2026-03-13T13:00:00Z",
                "ends_at": "2026-03-13T15:00:00Z",
                "is_busy": True,
            },
            {
                "canonical_table": "conversation_messages",
                "message_id": "msg-001",
                "sender_name": "Sara Chen",
                "is_from_self": False,
                "content": "Can you intro me to Marcus about the edtech pilot?",
            },
        ],
    )
    recompute_all_gate_aggregates(SignalObjectStore(conn))


def test_evaluate_introduction_facets() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_harness(conn)
    result = evaluate_opportunity(
        conn,
        "evaluate_introduction",
        context={"domain_tags": ["edtech", "intro"]},
    )
    assert result["opportunity_type"] == "evaluate_introduction"
    assert len(result["facet_results"]) == 5
    assert "composite_score" in result
    assert result["confidence_band"] in {"high", "medium", "low"}


def test_schedule_meeting_overlap_mar16() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_harness(conn)
    result = evaluate_opportunity(conn, "schedule_meeting")
    timing = next(f for f in result["facet_results"] if f["facet_id"] == "timing_feasibility")
    assert timing["score"] >= 0.8


def test_x03_harness_scenario() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_harness(conn)
    result = evaluate_opportunity(
        conn,
        "evaluate_introduction",
        context={"domain_tags": ["edtech", "intro", "marcus"]},
    )
    assert result["composite_score"] >= 0.55


def test_fit_readiness_after_seed() -> None:
    conn = sqlite3.connect(":memory:")
    _seed_harness(conn)
    readiness = compute_fit_readiness(conn)
    assert readiness["schedule_meeting"] >= 0.8
    assert readiness["evaluate_introduction"] >= 0.8
