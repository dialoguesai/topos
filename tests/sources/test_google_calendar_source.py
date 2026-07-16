"""Google Calendar canonical lane: registry definition, parser, and mapper.

PLAN_CANONICAL_CALENDAR_DOCUMENTS Part B. Exercises the availability layer
(is_busy from transparency/status, is_all_day from date-vs-dateTime,
self_response_status, is_organizer, is_recurring) and every
attendance_priority / movability_score / value_score branch from the spec.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.canonicalization.mappers import MAPPER_REGISTRY
from topos.canonicalization.mappers.google_calendar_mapper import GoogleCalendarMapper
from topos.ingestion.parsers import GoogleCalendarParser, PARSER_REGISTRY
from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.sources.base import RawRecord
from topos.sources.bundled_canonical_triples import infer_bundled_canonical_triple
from topos.sources.definitions import accepts_app_ingest
from topos.sources.registry import GCAL_EVENTS, REGISTRY
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.db.migrations import apply_all_migrations


def _gcal_event(**overrides) -> dict:
    """Google Calendar `events` resource-shaped record (extra fields kept)."""
    event = {
        "id": "evt1",
        "status": "confirmed",
        "transparency": "opaque",
        "summary": "Team Meeting",
        "eventType": "default",
        "start": {"dateTime": "2026-07-01T10:00:00-07:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-07-01T10:30:00-07:00", "timeZone": "America/Los_Angeles"},
        "organizer": {"email": "jonny@example.com", "self": True},
        "attendees": [
            {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
            {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
        ],
        "htmlLink": "https://calendar.google.com/event?eid=evt1",
        "location": "Room 1",
        "description": "weekly meeting",
        "created": "2026-06-01T00:00:00Z",
        "updated": "2026-06-25T00:00:00Z",
    }
    event.update(overrides)
    return event


def _map(event: dict) -> dict:
    normalized = NormalizedRecord(record_id=str(event.get("id")), payload=event)
    return GoogleCalendarMapper().map(normalized).payload


# --- registry / parser / triple wiring --------------------------------------


def test_registry_definition() -> None:
    assert REGISTRY["gcal_events"] is GCAL_EVENTS
    assert GCAL_EVENTS.source_type == "ui_stream"
    assert GCAL_EVENTS.delivery == "client_push"
    assert GCAL_EVENTS.schema_id == "gcal.events.v1"
    assert GCAL_EVENTS.parser_id == "gcal.events.v1"
    assert GCAL_EVENTS.canonical_group_id == "schedule"
    assert GCAL_EVENTS.canonical_mapper_id == "google_calendar"
    assert GCAL_EVENTS.signal_derivation_jobs == ["availability_scores"]
    assert GCAL_EVENTS.allowed_scope_ids == ["schedule:read", "availability:read"]
    assert accepts_app_ingest(GCAL_EVENTS)


def test_bundled_triple_and_registries() -> None:
    assert infer_bundled_canonical_triple(schema_id="gcal.events.v1") == (
        "google_calendar",
        "schedule",
    )
    assert PARSER_REGISTRY["gcal.events.v1"] is GoogleCalendarParser
    assert MAPPER_REGISTRY["google_calendar"] is GoogleCalendarMapper


def test_parser_validate_and_parse() -> None:
    parser = GoogleCalendarParser(dataset_id="user:default:device")
    missing = _gcal_event()
    missing.pop("id")
    assert not parser.validate(RawRecord(record_id="r-1", payload=missing)).is_valid
    assert parser.validate(RawRecord(record_id="r-1", payload=_gcal_event())).is_valid

    normalized = parser.parse(RawRecord(record_id="r-1", payload=_gcal_event()))
    assert normalized.record_id == "evt1"
    assert normalized.payload["summary"] == "Team Meeting"
    assert normalized.payload["dataset_id"] == "user:default:device"
    assert normalized.payload["status"] == "confirmed"


# --- availability layer ------------------------------------------------------


def test_event_id_is_deterministic_with_calendar_id() -> None:
    mapped = _map(_gcal_event(calendar_id="work@group.calendar.google.com"))
    assert mapped["event_id"] == "gcal:work@group.calendar.google.com:evt1"

    mapped_default = _map(_gcal_event())
    assert mapped_default["event_id"] == "gcal:primary:evt1"


@pytest.mark.parametrize(
    ("overrides", "expected_is_busy"),
    [
        ({}, True),  # opaque (default) = busy
        ({"transparency": "opaque"}, True),
        ({"transparency": "transparent"}, False),
        ({"status": "cancelled"}, False),
        ({"transparency": "transparent", "status": "cancelled"}, False),
    ],
)
def test_is_busy_from_transparency_and_status(overrides, expected_is_busy) -> None:
    mapped = _map(_gcal_event(**overrides))
    assert mapped["is_busy"] is expected_is_busy


def test_is_all_day_from_date_vs_datetime() -> None:
    all_day = _map(
        _gcal_event(start={"date": "2026-07-04"}, end={"date": "2026-07-05"})
    )
    assert all_day["is_all_day"] is True
    assert all_day["starts_at"] == "2026-07-04"

    timed = _map(_gcal_event())
    assert timed["is_all_day"] is False
    assert timed["starts_at"] == "2026-07-01T10:00:00-07:00"
    assert timed["timezone"] == "America/Los_Angeles"


def test_self_response_status_reads_self_attendee() -> None:
    tentative = _map(
        _gcal_event(
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "tentative"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ]
        )
    )
    assert tentative["self_response_status"] == "tentative"

    solo = _map(_gcal_event(attendees=[]))
    assert solo["self_response_status"] is None


def test_is_organizer_from_organizer_self() -> None:
    organizer = _map(_gcal_event())
    assert organizer["is_organizer"] is True

    guest = _map(
        _gcal_event(
            organizer={"email": "other@example.com", "self": False},
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert guest["is_organizer"] is False


def test_availability_passthrough_fields() -> None:
    mapped = _map(_gcal_event(recurringEventId="series-1"))
    assert mapped["is_recurring"] is True
    assert mapped["event_type"] == "default"
    assert mapped["location"] == "Room 1"
    assert mapped["description"] == "weekly meeting"
    assert mapped["url"] == "https://calendar.google.com/event?eid=evt1"
    assert mapped["attendee_count"] == 2
    assert mapped["accepted_count"] == 2
    assert mapped["created_at"] == "2026-06-01T00:00:00Z"
    assert mapped["updated_at"] == "2026-06-25T00:00:00Z"

    not_recurring = _map(_gcal_event())
    assert not_recurring["is_recurring"] is False


# --- attendance_priority branches --------------------------------------------


def test_priority_skip_when_self_declined() -> None:
    mapped = _map(
        _gcal_event(
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "declined"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ]
        )
    )
    assert mapped["attendance_priority"] == "skip"
    assert mapped["value_reason"] == "you declined this event"


def test_priority_skip_when_transparent() -> None:
    mapped = _map(_gcal_event(transparency="transparent"))
    assert mapped["attendance_priority"] == "skip"
    assert mapped["value_reason"] == "marked as free time (transparent)"


def test_priority_skip_when_cancelled() -> None:
    mapped = _map(_gcal_event(status="cancelled"))
    assert mapped["attendance_priority"] == "skip"
    assert mapped["value_reason"] == "event was cancelled"


def test_priority_must_attend_when_organizer_of_group_meeting() -> None:
    mapped = _map(
        _gcal_event(
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "a@example.com", "self": False, "responseStatus": "accepted"},
                {"email": "b@example.com", "self": False, "responseStatus": "needsAction"},
            ]
        )
    )
    assert mapped["attendance_priority"] == "must_attend"
    assert mapped["value_reason"] == "you organize this with 2 guests"


def test_priority_attend_when_accepted_guest_of_group_meeting() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "other@example.com", "self": False},
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert mapped["attendance_priority"] == "attend"
    assert mapped["value_reason"] == "you accepted, 2 attendees"


@pytest.mark.parametrize("response_status", ["needsAction", "tentative"])
def test_priority_optional_when_undecided(response_status) -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "other@example.com", "self": False},
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": response_status},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert mapped["attendance_priority"] == "optional"
    assert mapped["value_reason"] == "you haven't responded yet"


def test_priority_optional_when_all_day_and_solo() -> None:
    mapped = _map(
        _gcal_event(
            start={"date": "2026-07-04"},
            end={"date": "2026-07-05"},
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["attendance_priority"] == "optional"
    assert mapped["value_reason"] == "all-day personal block"


def test_priority_defaults_to_attend_for_solo_timed_block() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["attendance_priority"] == "attend"


# --- movability_score branches -----------------------------------------------


def test_movability_skip_is_fully_movable() -> None:
    mapped = _map(_gcal_event(status="cancelled"))
    assert mapped["movability_score"] == 1.0


def test_movability_solo_self_block() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["movability_score"] == pytest.approx(0.7)


def test_movability_solo_recurring_adds_bonus() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
            recurringEventId="series-1",
        )
    )
    assert mapped["movability_score"] == pytest.approx(0.9)


def test_movability_organizer_small_meeting() -> None:
    mapped = _map(
        _gcal_event(
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "a@example.com", "self": False, "responseStatus": "accepted"},
                {"email": "b@example.com", "self": False, "responseStatus": "accepted"},
            ]
        )
    )
    assert mapped["attendance_priority"] == "must_attend"
    assert mapped["movability_score"] == pytest.approx(0.8)


def test_movability_organizer_large_meeting() -> None:
    attendees = [{"email": "jonny@example.com", "self": True, "responseStatus": "accepted"}] + [
        {"email": f"guest{i}@example.com", "self": False, "responseStatus": "accepted"}
        for i in range(5)
    ]
    mapped = _map(_gcal_event(attendees=attendees))
    assert mapped["attendee_count"] == 6
    assert mapped["movability_score"] == pytest.approx(0.4)


def test_movability_guest_large_meeting() -> None:
    attendees = [{"email": "jonny@example.com", "self": True, "responseStatus": "accepted"}] + [
        {"email": f"guest{i}@example.com", "self": False, "responseStatus": "accepted"}
        for i in range(5)
    ]
    mapped = _map(
        _gcal_event(organizer={"email": "other@example.com", "self": False}, attendees=attendees)
    )
    assert mapped["is_organizer"] is False
    assert mapped["attendee_count"] == 6
    assert mapped["movability_score"] == pytest.approx(0.15)


def test_movability_guest_small_meeting() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "other@example.com", "self": False},
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert mapped["is_organizer"] is False
    assert mapped["attendee_count"] == 2
    assert mapped["movability_score"] == pytest.approx(0.5)


# --- value_score branches -----------------------------------------------------


def test_value_score_baseline_must_attend() -> None:
    mapped = _map(
        _gcal_event(
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "a@example.com", "self": False, "responseStatus": "accepted"},
            ]
        )
    )
    assert mapped["attendance_priority"] == "must_attend"
    assert mapped["value_score"] == pytest.approx(0.8)


def test_value_score_baseline_attend() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["attendance_priority"] == "attend"
    assert mapped["value_score"] == pytest.approx(0.5)


def test_value_score_baseline_optional() -> None:
    mapped = _map(
        _gcal_event(
            organizer={"email": "other@example.com", "self": False},
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "tentative"},
                {"email": "other@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert mapped["attendance_priority"] == "optional"
    assert mapped["value_score"] == pytest.approx(0.3)


def test_value_score_baseline_skip() -> None:
    mapped = _map(_gcal_event(status="cancelled"))
    assert mapped["attendance_priority"] == "skip"
    assert mapped["value_score"] == pytest.approx(0.1)


def test_value_score_high_value_keyword_bump() -> None:
    mapped = _map(
        _gcal_event(
            summary="Job Interview with candidate",
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["attendance_priority"] == "attend"
    assert mapped["value_score"] == pytest.approx(0.7)  # 0.5 + 0.2


def test_value_score_low_value_keyword_bump() -> None:
    mapped = _map(
        _gcal_event(
            summary="Daily Standup",
            organizer={"email": "jonny@example.com", "self": True},
            attendees=[],
        )
    )
    assert mapped["attendance_priority"] == "attend"
    assert mapped["value_score"] == pytest.approx(0.4)  # 0.5 - 0.1


def test_value_score_clamped_at_upper_bound() -> None:
    mapped = _map(
        _gcal_event(
            summary="Flight to NYC",
            attendees=[
                {"email": "jonny@example.com", "self": True, "responseStatus": "accepted"},
                {"email": "a@example.com", "self": False, "responseStatus": "accepted"},
            ],
        )
    )
    assert mapped["attendance_priority"] == "must_attend"
    assert mapped["value_score"] == pytest.approx(1.0)  # 0.8 + 0.2, clamped


def test_priority_confidence_is_pure_heuristic() -> None:
    mapped = _map(_gcal_event())
    assert mapped["priority_confidence"] == pytest.approx(0.5)


# --- DB round-trip ------------------------------------------------------------


def test_gcal_event_maps_to_calendar_events_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)

    payload = _map(_gcal_event())
    payload["source_id"] = "gcal_events"

    store = SQLiteCanonicalStore(conn)
    ref = store.upsert("calendar_events", payload, sync_batch_id="gcal-batch-1")
    assert ref.created is True

    row = conn.execute(
        """
        SELECT event_id, title, is_busy, is_organizer, attendance_priority,
               movability_score, value_score, source_id, sync_batch_id
        FROM calendar_events WHERE event_id=?
        """,
        (payload["event_id"],),
    ).fetchone()
    assert row is not None
    assert row[1] == "Team Meeting"
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == "must_attend"
    assert row[5] == pytest.approx(0.8)
    assert row[6] == pytest.approx(0.8)
    assert row[7] == "gcal_events"
    assert row[8] == "gcal-batch-1"

    # Idempotent re-ingest: same event_id updates in place, no duplicate row.
    ref2 = store.upsert("calendar_events", payload, sync_batch_id="gcal-batch-2")
    assert ref2.created is False
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1
