"""When an evidence row happened, for tables whose writers did not record a zone (OD-50, OD-53).

A grant's rolling window compares a row's event time with the request's clock, and
``fact_eligibility.canonical_utc_microseconds`` accepts only explicit UTC text: anything else is
"missing or ambiguous: withhold". That is right for messages, whose writers store UTC. The
journal writer stores what the owner's app sent: naive local text with no zone. Under the
canonical rule every journal row is undated, so no journal row could ever be inside a window.

Reading naive text as UTC would be a guess (wrong by up to 14 hours east and 12 west), and the
node's own zone at ingest is not the zone the entry was written in. What a naive stamp does
state without any guess is its calendar day. ``stated_day_v1`` takes exactly that:

- explicit UTC text is an instant, as under the canonical rule;
- naive ISO text (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS[.ffffff]``) is its stated day. The day
  began no earlier than 00:00 at UTC+14 and has ended everywhere by 12:00 UTC the next day
  (``STATED_DAY_ELAPSED_MICROSECONDS``, the same edge a stated-day fact uses);
- a row is inside a window only when that whole span is. So the newest entries wait until their
  day has ended everywhere, and an entry near the old edge leaves the window a little early. Both
  errors withhold; neither releases;
- the time a grant may release for such a row is its stated day, never an instant.

Text with any other offset, a calendar-invalid date, padding or another type stays unknown under
both rules. Nothing here rewrites a row: the rule is auditable from the stored text alone.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .fact_eligibility import STATED_DAY_ELAPSED_MICROSECONDS, canonical_utc_microseconds

CANONICAL = "canonical_event_time_v1"
STATED_DAY = "stated_day_v1"
SEMANTICS = (CANONICAL, STATED_DAY)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NAIVE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)?")
DAY_MICROSECONDS = 86_400 * 1_000_000
# A calendar day begins first at UTC+14: fourteen hours before its own 00:00 UTC.
STATED_DAY_EARLIEST_MICROSECONDS = -14 * 3600 * 1_000_000


def _stated_day_start(value) -> int | None:
    """00:00 UTC of the day a naive stamp states, in microseconds; None when it states none."""
    if type(value) is not str:
        return None
    match = _NAIVE.fullmatch(value)
    if match is None:
        return None
    try:
        datetime.fromisoformat(value)  # a real time of day, when one is written
        day = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (day - _EPOCH).days * DAY_MICROSECONDS


def event_bounds(value, *, semantics: str) -> tuple[int, int] | None:
    """(earliest, latest) instant the row can have happened, in UTC microseconds, or None when unknown."""
    if semantics not in SEMANTICS:
        raise ValueError("unknown event time semantics")
    instant = canonical_utc_microseconds(value)
    if instant is not None:
        return instant, instant
    if semantics != STATED_DAY:
        return None
    start = _stated_day_start(value)
    if start is None:
        return None
    return start + STATED_DAY_EARLIEST_MICROSECONDS, start + STATED_DAY_ELAPSED_MICROSECONDS


def within_window(value, *, semantics: str, lower_us: int, upper_us: int) -> bool:
    """Whether every instant the row can have happened lies inside [lower_us, upper_us]."""
    bounds = event_bounds(value, semantics=semantics)
    return bounds is not None and lower_us <= bounds[0] and bounds[1] <= upper_us


def released_time(value, *, semantics: str, precision: str) -> int | None:
    """The event time a grant may release for this row, in UTC seconds, at the grant's precision.

    ``none`` releases nothing. An instant releases as the canonical rule releases it: itself at
    ``second``, its UTC day at ``day``. A stated day releases only at ``day``, as that day's 00:00
    UTC; at ``second`` it releases nothing, because the row never stated an instant.
    """
    if precision not in ("none", "day", "second"):
        raise ValueError("unknown time precision")
    if precision == "none":
        return None
    instant = canonical_utc_microseconds(value)
    if instant is not None:
        return instant // 1_000_000 if precision == "second" else instant // DAY_MICROSECONDS * 86_400
    if semantics != STATED_DAY or precision != "day":
        return None
    start = _stated_day_start(value)
    return None if start is None else start // 1_000_000
