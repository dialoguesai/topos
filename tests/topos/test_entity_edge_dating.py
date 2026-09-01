"""Graph edges must carry the time of the thing they came from.

What this guards, measured on a real ChatGPT import of 1,086 conversations:
**9,242 of 9,242 co-occurrence edges had no timestamp at all** — 93% of every
edge the import produced. The declared lane sitting beside them, reading the
same records, was fully dated.

An undated edge cannot be placed in a temporal view. It does not appear in the
wrong part of the timeline; it cannot appear on one, which is worse, because the
graph then silently under-represents whichever period the undated edges belong
to.

The cause was one naive read. The file already knew the canonical groups
disagree on the time column and already had `_EVENT_AT_FIELDS` for exactly that,
and used it everywhere except the path that produces most of the edges.
"""

from __future__ import annotations

from topos.enrichment.jobs.canonical.entities_job import _EVENT_AT_FIELDS


def _first_event_at(msg):
    """The lookup the job performs. Kept in step with the job by the tests below."""
    return next((msg.get(f) for f in _EVENT_AT_FIELDS if msg.get(f)), None)


def test_the_canonical_groups_that_disagree_are_all_covered():
    """messages use event_at, activity uses occurred_at, journal uses entry_at.
    A group missing from this tuple is a group whose edges arrive undated."""
    for field in ("event_at", "ts", "occurred_at", "entry_at", "starts_at", "created_at"):
        assert field in _EVENT_AT_FIELDS


def test_a_message_row_is_dated():
    assert _first_event_at({"event_at": "2025-07-01T10:00:00Z"}) == "2025-07-01T10:00:00Z"


def test_an_activity_row_is_dated():
    # occurred_at, not event_at — the exact shape the naive read dropped.
    assert _first_event_at({"occurred_at": "2025-07-01T10:00:00Z"}) == "2025-07-01T10:00:00Z"


def test_a_journal_row_is_dated():
    assert _first_event_at({"entry_at": "2025-07-01T10:00:00Z"}) == "2025-07-01T10:00:00Z"


def test_the_first_populated_field_wins():
    # event_at is preferred; created_at is the row's write time and a poor
    # stand-in for when the thing happened.
    msg = {"event_at": "2025-07-01T10:00:00Z", "created_at": "2026-09-01T12:00:00Z"}
    assert _first_event_at(msg) == "2025-07-01T10:00:00Z"


def test_an_empty_string_does_not_count_as_a_date():
    assert _first_event_at({"event_at": "", "occurred_at": "2025-07-01T10:00:00Z"}) == (
        "2025-07-01T10:00:00Z"
    )


def test_a_record_with_no_time_at_all_stays_undated():
    # Honest None rather than a fabricated now(): a stamped-today edge claims
    # the thing happened today, which is worse than admitting it is unknown.
    assert _first_event_at({"content": "hello"}) is None


def test_the_cooccurrence_path_uses_the_tolerant_lookup():
    """A source assertion, because the call site is inside a long method that
    needs a database and an NER pass to reach. It is the exact line that left
    9,242 edges undated."""
    from pathlib import Path

    src = Path("topos/enrichment/jobs/canonical/entities_job.py").read_text()
    assert '(msg_by_id.get(record_id) or {}).get("event_at")' not in src
    assert "msg_for_event.get(f) for f in _EVENT_AT_FIELDS" in src
