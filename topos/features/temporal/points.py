"""Time points with explicit precision, timezone basis and provenance.

A point is what a producer actually knows about a time, and no more. A stated
day stays a day, a year stays a year, a naive clock reading keeps an unrecorded
timezone, and a missing time stays unknown. Nothing here rounds, truncates or
defaults a value into a more precise form.

Comparisons are three-valued. ``None`` means "cannot tell" and is never
promoted to a decision by this module.

The grammar is this module's own, in integer arithmetic. It deliberately does
not use ``datetime.fromisoformat``: which fractional-second lengths that accepts
depends on the Python version (3.10 accepts only 0, 3 or 6 digits), so a
contract built on it can decide differently on two interpreters. See
``TEMPORAL_FIELDS.md`` for the design and the parity it keeps with the signed
permission time contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Optional

Precision = Literal["instant", "day", "month", "year", "unknown"]
Basis = Literal["utc", "fixed_offset", "unrecorded", "unknown"]
Provenance = Literal[
    "native_source_clock", "stated_in_content", "producer_clock", "owner_edit",
    "ingestion_clock_substitute", "unverified_producer", "unknown",
]
PRECISIONS = ("instant", "day", "month", "year", "unknown")
BASES = ("utc", "fixed_offset", "unrecorded", "unknown")
PROVENANCES = ("native_source_clock", "stated_in_content", "producer_clock", "owner_edit",
               "ingestion_clock_substitute", "unverified_producer", "unknown")

MICROSECONDS_PER_SECOND = 1_000_000
MICROSECONDS_PER_DAY = 86_400 * MICROSECONDS_PER_SECOND
# Civil offsets in use since standard time span UTC-12 to UTC+14. A local
# reading L with an unrecorded offset therefore denotes some UTC instant in
# [L - 14h, L + 12h]. Local mean time before standard time could fall outside
# this range; a contract that needs that certainty for early dates must treat
# unrecorded points before 1900 as unknown under its own contract id.
EAST_MOST_OFFSET_MINUTES = 14 * 60
WEST_MOST_OFFSET_MINUTES = -12 * 60
_EAST = EAST_MOST_OFFSET_MINUTES * 60 * MICROSECONDS_PER_SECOND
_WEST = -WEST_MOST_OFFSET_MINUTES * 60 * MICROSECONDS_PER_SECOND

_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})?"
)
_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_MONTH = re.compile(r"([0-9]{4})-([0-9]{2})")
_YEAR = re.compile(r"([0-9]{4})")


def days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar, exact for any year."""
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    return (31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def _valid_date(year: int, month: int, day: int) -> bool:
    return 1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= _days_in_month(year, month)


@dataclass(frozen=True)
class TimePoint:
    """One time as a producer knows it. Construct through ``parse_point`` or ``unknown``."""
    text: Optional[str]
    precision: Precision
    basis: Basis
    offset_minutes: Optional[int]
    provenance: Provenance

    def __post_init__(self):
        if (self.precision not in PRECISIONS or self.basis not in BASES
                or self.provenance not in PROVENANCES):
            raise ValueError("time point vocabulary")
        if (self.precision == "unknown") != (self.text is None) or (self.precision == "unknown") != (self.basis == "unknown"):
            raise ValueError("unknown time point must carry no value and no basis")
        if (self.basis == "fixed_offset") != (self.offset_minutes is not None):
            raise ValueError("offset belongs to a fixed_offset basis only")
        if self.offset_minutes is not None and (type(self.offset_minutes) is not int
                or not WEST_MOST_OFFSET_MINUTES <= self.offset_minutes <= EAST_MOST_OFFSET_MINUTES):
            raise ValueError("offset outside civil range")
        if self.precision != "unknown" and _classify(self.text) != (self.precision, self.basis, self.offset_minutes):
            raise ValueError("time point fields disagree with its text")

    @property
    def known(self) -> bool:
        return self.precision != "unknown"

    def to_json(self) -> dict:
        return {"text": self.text, "precision": self.precision, "basis": self.basis,
                "offset_minutes": self.offset_minutes, "provenance": self.provenance}

    @classmethod
    def from_json(cls, value) -> "TimePoint":
        if type(value) is not dict or set(value) != {"text", "precision", "basis", "offset_minutes", "provenance"}:
            raise ValueError("time point shape")
        return cls(value["text"], value["precision"], value["basis"], value["offset_minutes"], value["provenance"])


def unknown(provenance: Provenance = "unknown") -> TimePoint:
    return TimePoint(None, "unknown", "unknown", None, provenance)


def _classify(raw):
    """``(precision, basis, offset_minutes)`` for text in the grammar, else ``None``."""
    if type(raw) is not str:
        return None
    match = _INSTANT.fullmatch(raw)
    if match:
        year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
        suffix = match.group(8)
        if not _valid_date(year, month, day) or hour > 23 or minute > 59 or second > 59:
            return None
        if suffix is None:
            return ("instant", "unrecorded", None)
        if suffix in ("Z", "+00:00", "-00:00"):
            return ("instant", "utc", None)
        sign = 1 if suffix[0] == "+" else -1
        hours, minutes = int(suffix[1:3]), int(suffix[4:6])
        offset = sign * (hours * 60 + minutes)
        if minutes > 59 or not WEST_MOST_OFFSET_MINUTES <= offset <= EAST_MOST_OFFSET_MINUTES:
            return None
        return ("instant", "fixed_offset", offset)
    match = _DAY.fullmatch(raw)
    if match:
        year, month, day = (int(group) for group in match.groups())
        return ("day", "unrecorded", None) if _valid_date(year, month, day) else None
    match = _MONTH.fullmatch(raw)
    if match:
        year, month = (int(group) for group in match.groups())
        return ("month", "unrecorded", None) if _valid_date(year, month, 1) else None
    match = _YEAR.fullmatch(raw)
    if match:
        return ("year", "unrecorded", None) if 1 <= int(match.group(1)) <= 9999 else None
    return None


def parse_point(raw, *, provenance: Provenance) -> TimePoint:
    """The point a lexical value denotes, or an unknown point. Never raises on input.

    Accepted: an instant with ``Z``, ``+00:00``, ``-00:00`` or a civil numeric
    offset; a naive instant (timezone unrecorded); ``YYYY-MM-DD``; ``YYYY-MM``;
    ``YYYY``. Leap seconds, out-of-range fields, calendar-invalid dates, epoch
    numbers, padded or partial text and anything else are unknown. ``-00:00``
    follows RFC 3339: the UTC instant is known, the local offset is not.
    """
    if provenance not in PROVENANCES:
        raise ValueError("time point provenance")
    classified = _classify(raw)
    if classified is None:
        return unknown(provenance)
    precision, basis, offset = classified
    return TimePoint(raw, precision, basis, offset, provenance)


@dataclass(frozen=True)
class Span:
    """Every UTC microsecond a point could refer to: ``[lo, hi]`` or ``[lo, hi)``."""
    lo: int
    hi: int
    hi_inclusive: bool

    def ends_at_or_before(self, instant: int) -> bool:
        return self.hi <= instant

    def ends_before(self, instant: int) -> bool:
        return self.hi < instant if self.hi_inclusive else self.hi <= instant


def span(point: TimePoint) -> Optional[Span]:
    if not point.known:
        return None
    text = point.text
    if point.precision == "instant":
        match = _INSTANT.fullmatch(text)
        year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
        fraction = int((match.group(7) or "").ljust(6, "0") or 0)
        local = ((days_from_civil(year, month, day) * 86_400 + hour * 3_600 + minute * 60 + second)
                 * MICROSECONDS_PER_SECOND + fraction)
        if point.basis == "utc":
            return Span(local, local, True)
        if point.basis == "fixed_offset":
            exact = local - point.offset_minutes * 60 * MICROSECONDS_PER_SECOND
            return Span(exact, exact, True)
        return Span(local - _EAST, local + _WEST, True)
    if point.precision == "day":
        year, month, day = (int(part) for part in text.split("-"))
        start = days_from_civil(year, month, day)
        end = start + 1
    elif point.precision == "month":
        year, month = (int(part) for part in text.split("-"))
        start = days_from_civil(year, month, 1)
        end = start + _days_in_month(year, month)
    else:
        year = int(text)
        start = days_from_civil(year, 1, 1)
        end = days_from_civil(year + 1, 1, 1)
    lo, hi = start * MICROSECONDS_PER_DAY, end * MICROSECONDS_PER_DAY
    # Day, month and year points carry an unrecorded basis by construction:
    # the grammar has no way to state an offset for them.
    return Span(lo - _EAST, hi + _WEST, False)


def order(a: TimePoint, b: TimePoint) -> Optional[Literal["before", "after", "overlapping"]]:
    """``before`` only when all of ``a`` ends before any of ``b`` could begin."""
    left, right = span(a), span(b)
    if left is None or right is None:
        return None
    if left.ends_before(right.lo):
        return "before"
    if right.ends_before(left.lo):
        return "after"
    return "overlapping"


def occurred_by(point: TimePoint, anchor_us: int) -> Optional[bool]:
    """Whether every instant the point could denote is at or before the anchor.

    For an explicit-UTC instant this is ``t <= anchor``, exactly the
    ``exact_instant_v1`` currency rule. For a day with an unrecorded basis it is
    ``anchor >= (D+1) 12:00Z``, exactly ``stated_day_v1``'s ``current_from``.
    """
    if type(anchor_us) is not int:
        raise ValueError("anchor must be integer microseconds")
    covered = span(point)
    if covered is None:
        return None
    return covered.hi <= anchor_us


def within(point: TimePoint, lower_us: int, upper_us: int) -> Optional[bool]:
    """Plain interval membership, making no claim to match any signed contract.

    ``True`` only when the whole point lies in ``[lower, upper]``, ``False`` only
    when none of it does. A permission time match must use ``event_window``
    instead: a signed deny rule treats a future event as unknown, and this
    function would call it outside, letting the deny silently not match.
    """
    if type(lower_us) is not int or type(upper_us) is not int or lower_us > upper_us:
        raise ValueError("window must be ordered integer microseconds")
    covered = span(point)
    if covered is None:
        return None
    last = covered.hi if covered.hi_inclusive else covered.hi - 1
    if covered.lo >= lower_us and last <= upper_us:
        return True
    if last < lower_us or covered.lo > upper_us:
        return False
    return None


def event_window(point: TimePoint, anchor_us: int, max_age_us: int) -> Optional[bool]:
    """The ``canonical_event_time_v1`` contributor window, three-valued.

    ``None`` when the point is unknown or could lie after the anchor; ``False``
    only when all of it ends before ``anchor - max_age``; ``True`` only when all
    of it lies inside ``[anchor - max_age, anchor]``; otherwise ``None``. For an
    explicit-UTC instant this is exactly the signed rule
    ``None if event > anchor else lower <= event``.
    """
    if type(anchor_us) is not int or type(max_age_us) is not int or max_age_us < 0:
        raise ValueError("window must be integer microseconds")
    covered = span(point)
    if covered is None:
        return None
    last = covered.hi if covered.hi_inclusive else covered.hi - 1
    if last > anchor_us:
        return None
    lower = anchor_us - max_age_us
    if last < lower:
        return False
    return True if covered.lo >= lower else None
