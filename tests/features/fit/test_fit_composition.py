"""Tests for fit composition layer."""

from __future__ import annotations

import sqlite3

from topos.features.fit.evaluator import (compute_fit_readiness, evaluate_opportunity,
                                          _evaluate_facet)
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


def test_an_unknown_warmth_band_is_not_evidence_of_a_warm_network():
    """The extractor cannot measure warmth from one record, so it stores "unknown".

    This facet used to score on the PRESENCE of any band, which meant the constant
    the extractor stamped on every edge was the entire evidence base for calling
    the owner's network warm. All 216 live edges read "medium" and the facet
    reported warm_network on that alone.
    """
    import json as _json

    def facet_for(bands):
        conn = sqlite3.connect(":memory:")
        apply_signal_objects_up(conn)
        apply_extraction_artifacts_up(conn)
        # Inserted directly: SignalObjectStore.upsert_object's UPDATE path touches
        # `updated_by`, which this migration does not create (the live schema does),
        # and _seed_harness already leaves a warmth_score behind to collide with.
        conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
            " object_key, payload_json, confidence, valid_from, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("w1", "relationships", "warmth_score", "aggregate",
             _json.dumps({"edge_count": 216, "warmth_bands": bands}), 0.85,
             "2026-08-26T00:00:00Z", "2026-08-26T00:00:00Z", "2026-08-26T00:00:00Z"),
        )
        conn.commit()
        return _evaluate_facet(SignalObjectStore(conn), "relationship_warmth", None, {})

    unknown = facet_for(["unknown"])
    assert unknown["public_band"] == "cold_network"
    assert unknown["score"] == 0.2
    assert unknown["confidence"] == 0.3        # unknown carries no confidence either

    measured = facet_for(["high", "medium"])   # a real band still reads warm
    assert measured["public_band"] == "warm_network"
    assert measured["score"] == 0.75
