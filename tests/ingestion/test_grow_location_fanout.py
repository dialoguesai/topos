"""Tests for Grow location → location_events fan-out."""

from __future__ import annotations

from topos.ingestion.grow_location_fanout import (
    grow_location_event_from_journal,
    grow_location_signal_record,
)


def test_grow_location_event_from_journal_builds_linked_row() -> None:
    row = grow_location_event_from_journal(
        {
            "entry_id": "grow-157",
            "entry_at": "2026-06-13T11:15:00",
            "place_name": "NYC",
            "category": "Chill",
        },
        source_id="grow_data_file",
    )
    assert row is not None
    assert row["event_id"] == "grow-157-loc"
    assert row["place_name"] == "NYC"
    assert row["source_record_id"] == "grow-157"


def test_grow_location_event_skips_empty_place() -> None:
    assert grow_location_event_from_journal({"entry_id": "grow-1"}, source_id="grow_data_file") is None


def test_grow_location_signal_record_for_enrichment() -> None:
    loc = grow_location_event_from_journal(
        {"entry_id": "grow-161", "place_name": "Brooklyn- The Convent", "entry_at": "2026-06-14T16:00:00"},
        source_id="grow_data_file",
    )
    assert loc is not None
    signal = grow_location_signal_record(loc)
    assert signal["place_name"] == "Brooklyn- The Convent"
    assert signal["canonical_table"] == "location_events"
