"""Event-time precedence: an event's OWN start time outranks the entry stamp.

Grow-session rows canonicalize with entry_at = INGEST moment and the real
session time in starts_at; entry_at-first ordering collapsed months of goals,
stats and timeline rows onto the ingest day (2026-06-28 live)."""

from topos.features.stats.definitions import row_event_ts


def test_starts_at_outranks_entry_at():
    row = {"entry_at": "2026-06-28T23:28:45", "starts_at": "2026-05-05T09:45:00"}
    assert row_event_ts(row).isoformat().startswith("2026-05-05")


def test_event_at_still_wins_over_everything():
    row = {"event_at": "2026-04-01T00:00:00", "starts_at": "2026-05-05T09:45:00"}
    assert row_event_ts(row).isoformat().startswith("2026-04-01")


def test_entry_at_used_when_no_start(): 
    row = {"entry_at": "2026-06-01T10:00:00"}
    assert row_event_ts(row).isoformat().startswith("2026-06-01")
