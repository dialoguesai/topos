"""When an evidence row happened, for tables whose writers did not record a zone (OD-50, OD-53).

A grant's rolling window compares a row's event time with the request's clock, and
``fact_eligibility.canonical_utc_microseconds`` accepts only explicit UTC text: anything else is
"missing or ambiguous: withhold". That is right for messages, whose writers store UTC. The
journal writer stores what the owner's app sent: naive local text with no zone. Under the
canonical rule every journal row is undated, so no journal row could ever be inside a window.

Reading naive text as UTC would be a guess (wrong by up to 14 hours east and 12 west), and the
node's own zone at ingest is not the zone the entry was written in. The node already has a
grammar that says what such text does state without guessing: ``features/temporal/points.py``
(``TEMPORAL_FIELDS.md``). A naive instant has an *unrecorded* basis and denotes some instant in
``[L - 14h, L + 12h]``; a bare day denotes ``[D-1 10:00Z, D+1 12:00Z)``, the span
``stated_day_v1`` facts already use. This module is that grammar applied to an evidence row's
time, with two rules a family can declare:

- ``canonical_event_time_v1``: explicit UTC text only. The message rule, unchanged.
- ``stated_day_v1`` (the decision recorded for journal rows): an instant with a recorded offset is
  that instant; naive text, with or without a time of day, is the calendar day it states. The
  written time of day is not trusted: keeping it and spanning every offset around it would be
  tighter, was measured (one more entry inside a 90-day window of 326) and was declined.

Under both rules a row is inside a window only when its whole span is, so the newest rows wait
until their span has ended and a row near the old edge leaves early: both errors withhold. The
time a grant may release for a row without a recorded offset is its stated day, never an instant.
Month and year text, padding, impossible dates and other types stay unknown. Nothing here rewrites
a row: the rule is auditable from the stored text alone.
"""
from __future__ import annotations

from ..features.temporal.points import MICROSECONDS_PER_DAY, parse_point, span
from .fact_eligibility import canonical_utc_microseconds

CANONICAL = "canonical_event_time_v1"
STATED_DAY = "stated_day_v1"
SEMANTICS = (CANONICAL, STATED_DAY)
_PROVENANCE = "unverified_producer"   # nothing about such a row says which clock wrote it


def _point(value, semantics: str):
    """The point a row's time text denotes under `semantics`, or None when it denotes none."""
    if semantics not in SEMANTICS:
        raise ValueError("unknown event time semantics")
    if semantics == CANONICAL:
        return parse_point(value, provenance=_PROVENANCE) if canonical_utc_microseconds(value) is not None else None
    point = parse_point(value, provenance=_PROVENANCE)
    if point.precision == "instant" and point.basis == "unrecorded":
        point = parse_point(point.text[:10], provenance=_PROVENANCE)   # the day it states, not the time it wrote
    # A month or a year is too coarse to place a row in a rolling window.
    return point if point.precision in ("instant", "day") else None


def row_time_text(row: dict, *, column: str):
    """The text that says when `row` happened: its event-time record when the door wrote one, else its column.

    A row written through a source that declares its zone carries ``event_time_json``
    (``topos-event-time/v1``): the local reading with the zone's offset, an exact instant. It counts
    only while it still describes the row: a record whose local reading is not the column's text
    was computed from a time the row no longer has, and a record that does not parse is damage.
    Either way the row's time is unknown (None), never silently its column's day.
    """
    from ..features.temporal.records import EventTime

    raw = row.get("event_time_json")
    if raw is None:
        return row.get(column)
    try:
        point = EventTime.from_json(raw).event
    except (ValueError, TypeError):
        return None
    written = row.get(column)
    if (point.precision != "instant" or parse_point(written, provenance=_PROVENANCE).basis != "unrecorded"
            or parse_point(written, provenance=_PROVENANCE).precision != "instant"
            or not point.text.startswith(written)):
        return None
    suffix = point.text[len(written):]
    if (point.basis == "utc" and suffix == "Z") or (point.basis == "fixed_offset" and len(suffix) == 6):
        return point.text
    return None


def event_bounds(value, *, semantics: str) -> tuple[int, int] | None:
    """(earliest, latest) UTC microsecond the row can have happened, inclusive, or None when unknown."""
    point = _point(value, semantics)
    if point is None:
        return None
    covered = span(point)
    return covered.lo, covered.hi if covered.hi_inclusive else covered.hi - 1


def within_window(value, *, semantics: str, lower_us: int, upper_us: int) -> bool:
    """Whether every instant the row can have happened lies inside [lower_us, upper_us]."""
    bounds = event_bounds(value, semantics=semantics)
    return bounds is not None and lower_us <= bounds[0] and bounds[1] <= upper_us


def released_time(value, *, semantics: str, precision: str) -> int | None:
    """The event time a grant may release for this row, in UTC seconds, at the grant's precision.

    ``none`` releases nothing. An instant with a recorded offset releases as the message rule
    releases it: itself at ``second``, its UTC day at ``day``. A row without a recorded offset
    releases only at ``day``, as the 00:00 UTC of the day its text states; at ``second`` it releases
    nothing, because the row never proved an instant.
    """
    if precision not in ("none", "day", "second"):
        raise ValueError("unknown time precision")
    point = _point(value, semantics)
    if point is None or precision == "none":
        return None
    if point.basis != "unrecorded":
        instant = span(point).lo
        return instant // 1_000_000 if precision == "second" else instant // MICROSECONDS_PER_DAY * 86_400
    if precision != "day":
        return None
    stated_day = parse_point(point.text[:10], provenance=_PROVENANCE)
    # The day's own 00:00 UTC: its span begins fourteen hours earlier, at UTC+14.
    return (span(stated_day).lo + 14 * 3_600 * 1_000_000) // 1_000_000
