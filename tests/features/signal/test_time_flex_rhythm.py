"""Time-signal upgrade tests (PLAN_TIME_SIGNAL_UPGRADE): movability plumb-through,
flex halos, meeting load band, behavioral rhythm, and the grantee summary items."""

from __future__ import annotations

import sqlite3

from topos.features.fit.evaluator import evaluate_opportunity
from topos.features.signal.extraction.artifact_router import route_canonical_batch
from topos.features.signal.extraction.rule_extractors import extract_from_calendar
from topos.features.signal.signal_object_store import SignalObjectStore
from topos.features.signal.typed_stores.aggregates import recompute_time_aggregates
from topos.features.signal.typed_stores.rhythm import recompute_rhythm
from topos.query.retrieval import _load_time_summary_items
from topos.storage.db.migrations.extraction_artifacts import apply_extraction_artifacts_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_signal_objects_up(conn)
    apply_extraction_artifacts_up(conn)
    return conn


def _seed_calendar(conn: sqlite3.Connection) -> None:
    route_canonical_batch(
        conn,
        [
            {
                "canonical_table": "calendar_events",
                "event_id": "cal-free",
                "starts_at": "2026-07-27T09:00:00+00:00",
                "ends_at": "2026-07-27T10:00:00+00:00",
                "is_busy": False,
            },
            {
                # Solo recurring block — movable (soft boundary).
                "canonical_table": "calendar_events",
                "event_id": "cal-soft",
                "starts_at": "2026-07-28T13:00:00+00:00",
                "ends_at": "2026-07-28T15:00:00+00:00",
                "is_busy": True,
                "movability_score": 0.9,
                "attendance_priority": "optional",
            },
            {
                # Large meeting as guest — fixed.
                "canonical_table": "calendar_events",
                "event_id": "cal-hard",
                "starts_at": "2026-07-29T10:00:00+00:00",
                "ends_at": "2026-07-29T11:00:00+00:00",
                "is_busy": True,
                "movability_score": 0.15,
                "attendance_priority": "attend",
            },
        ],
    )


def test_extractor_carries_movability() -> None:
    drafts = extract_from_calendar(
        {
            "event_id": "e1",
            "starts_at": "2026-07-28T13:00:00Z",
            "ends_at": "2026-07-28T14:00:00Z",
            "is_busy": True,
            "movability_score": 0.8,
            "attendance_priority": "optional",
        }
    )
    payload = drafts[0][1]
    assert payload["hard_or_soft"] == "soft"
    assert payload["movability_band"] == "flexible"
    assert payload["attendance_priority"] == "optional"


def test_extractor_null_movability_stays_hard() -> None:
    drafts = extract_from_calendar(
        {
            "event_id": "e2",
            "starts_at": "2026-07-28T13:00:00Z",
            "ends_at": "2026-07-28T14:00:00Z",
            "is_busy": True,
        }
    )
    payload = drafts[0][1]
    assert payload["hard_or_soft"] == "hard"
    assert payload["movability_band"] is None


def test_time_aggregates_emit_flex_and_load() -> None:
    conn = _conn()
    _seed_calendar(conn)
    store = SignalObjectStore(conn)
    recompute_time_aggregates(store)

    blocks = store.list_objects("time", object_type="commitment_hard_blocks", limit=1)
    payload = blocks[0]["payload"]
    assert payload["hard_count"] == 1
    assert payload["soft_count"] == 1

    flex = store.list_objects("time", object_type="flex_windows", limit=1)
    assert flex, "negotiable busy block must emit a flex window"
    window = flex[0]["payload"]["windows"][0]
    assert window["kind"] == "conditional_availability"
    assert window["negotiability"] == "flexible"
    # 2h block → halo capped at 60 min on each side.
    assert window["flex_before"]["start"] == "2026-07-28T12:00:00+00:00"
    assert window["flex_after"]["end"] == "2026-07-28T16:00:00+00:00"

    load = store.list_objects("time", object_type="meeting_load_band", limit=1)
    assert load[0]["payload"]["band"] == "light"
    assert load[0]["payload"]["soft_count"] == 1

    summary = store.list_objects("time", object_type="availability_summary", limit=1)
    text = summary[0]["payload"]["summary_text"]
    assert "negotiable" in text


def test_rhythm_objects_from_behavior_tables() -> None:
    conn = _conn()
    conn.execute(
        "CREATE TABLE conversation_messages (message_id TEXT, event_at TEXT)"
    )
    conn.execute("CREATE TABLE activity_events (event_id TEXT, occurred_at TEXT)")
    # Three Tuesday mornings of messaging + three Wednesday evenings of browsing.
    for day, hour in (("2026-07-07", 9), ("2026-07-14", 10), ("2026-07-21", 9)):
        conn.execute(
            "INSERT INTO conversation_messages VALUES (?, ?)",
            (f"m-{day}", f"{day}T{hour:02d}:15:00+00:00"),
        )
    for day in ("2026-07-08", "2026-07-15", "2026-07-22"):
        conn.execute(
            "INSERT INTO activity_events VALUES (?, ?)",
            (f"a-{day}", f"{day}T19:30:00+00:00"),
        )
    store = SignalObjectStore(conn)
    created = recompute_rhythm(store, conn)
    assert created >= 3  # two patterns + routine_confidence

    patterns = {
        (o["payload"]["day_of_week"], o["payload"]["time_band"]): o["payload"]
        for o in store.list_objects("time", object_type="RoutinePattern", limit=50)
    }
    assert patterns[("tue", "morning")]["dominant_kind"] == "communication"
    assert patterns[("wed", "evening")]["dominant_kind"] == "browsing"

    routine = store.list_objects("time", object_type="routine_confidence", limit=1)
    payload = routine[0]["payload"]
    assert payload["sample_count"] == 6
    assert payload["span_days"] >= 15
    assert 0 < payload["confidence"] <= 0.95
    assert payload["top_bands"]


def test_rhythm_empty_db_is_noop() -> None:
    conn = _conn()
    assert recompute_rhythm(SignalObjectStore(conn), conn) == 0


def test_fit_negotiable_overlap_via_flex_halo() -> None:
    conn = _conn()
    _seed_calendar(conn)
    recompute_time_aggregates(SignalObjectStore(conn))
    # Target date has no free window, but the soft block's halo covers it.
    result = evaluate_opportunity(
        conn,
        "schedule_meeting",
        context={"target_window_start": "2026-07-28T12:30:00+00:00"},
    )
    timing = next(
        f for f in result["facet_results"] if f["facet_id"] == "timing_feasibility"
    )
    assert timing["public_band"] == "negotiable_overlap"
    assert timing["score"] == 0.6


def test_fit_target_date_respected() -> None:
    conn = _conn()
    _seed_calendar(conn)
    recompute_time_aggregates(SignalObjectStore(conn))
    result = evaluate_opportunity(
        conn,
        "schedule_meeting",
        context={"target_window_start": "2026-07-27T09:30:00+00:00"},
    )
    timing = next(
        f for f in result["facet_results"] if f["facet_id"] == "timing_feasibility"
    )
    assert timing["public_band"] == "overlap_found"
    # Load band replaces the raw busy-count heuristic.
    conflict = next(
        f for f in result["facet_results"] if f["facet_id"] == "commitment_conflict"
    )
    assert conflict["public_band"] == "light_load"


def test_time_summary_items_are_intent_proportional() -> None:
    conn = _conn()
    _seed_calendar(conn)
    recompute_time_aggregates(SignalObjectStore(conn))

    # A flexibility question serves the flex layer, not the whole bundle.
    flex_items = _load_time_summary_items(conn, "is any of their busy time movable?")
    assert {i["retrieval_source"] for i in flex_items} == {"flex_windows"}

    # A load question serves the load band only.
    load_items = _load_time_summary_items(conn, "how heavy is their meeting load?")
    assert {i["retrieval_source"] for i in load_items} == {"meeting_load_band"}

    # No aspect keywords ⇒ the compact digest alone — never the full bundle.
    default_items = _load_time_summary_items(conn, "tell me about tuesday")
    assert {i["retrieval_source"] for i in default_items} <= {"availability_summary"}

    blob = " ".join(i["summary_text"] for i in flex_items + load_items + default_items)
    # Bands cross the boundary; raw scores and titles never do.
    assert "0.9" not in blob
    assert "cal-soft" not in blob
