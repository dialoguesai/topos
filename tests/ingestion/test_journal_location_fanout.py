"""Tests for journal → location_events fan-out."""

from __future__ import annotations

from topos.ingestion.journal_location_fanout import (
    journal_location_event_from_entry,
    journal_location_signal_record,
)


def test_journal_location_event_from_entry_builds_linked_row() -> None:
    row = journal_location_event_from_entry(
        {
            "entry_id": "tl-157",
            "place_name": "Home",
            "starts_at": "2026-06-14T16:00:00",
            "category": "Topos",
        },
        source_id="time_log",
    )
    assert row is not None
    assert row["event_id"] == "tl-157-loc"
    assert row["place_name"] == "Home"
    assert row["source_record_id"] == "tl-157"


def test_journal_location_event_skips_empty_place() -> None:
    assert journal_location_event_from_entry({"entry_id": "tl-1"}, source_id="time_log") is None


def test_journal_location_signal_record_for_enrichment() -> None:
    loc = journal_location_event_from_entry(
        {"entry_id": "tl-161", "place_name": "Brooklyn- The Convent", "entry_at": "2026-06-14T16:00:00"},
        source_id="time_log",
    )
    assert loc is not None
    signal = journal_location_signal_record(loc)
    assert signal["canonical_table"] == "location_events"
    assert signal["place_name"] == "Brooklyn- The Convent"
