"""Versioned temporal records stored beside the legacy time columns.

``topos-fact-temporal/v1`` separates the three times a fact row used to share in
``valid_from``: when the node asserted it, when the stated thing applies in the
world, and when the source record it came from happened.
``topos-event-time/v1`` records a canonical message's event time with the
provenance of the clock that produced it.

Records are written once, when a row is inserted, and never rewritten. They are
stored as canonical JSON (sorted keys, no whitespace) so the same record always
has the same bytes, and they are parsed strictly: an unrecognised shape is an
error, not a partial record.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Optional

from .points import TimePoint, parse_point, unknown

FACT_TEMPORAL_VERSION = "topos-fact-temporal/v1"
EVENT_TIME_VERSION = "topos-event-time/v1"


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def producer_clock(now: Optional[datetime] = None, *, provenance: str = "producer_clock") -> TimePoint:
    """The asserting clock as an explicit-UTC instant: a producer's, or an owner's edit."""
    if provenance not in ("producer_clock", "owner_edit"):
        raise ValueError("an asserting clock is a producer clock or an owner edit")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("producer clock must be timezone-aware")
    text = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parse_point(text, provenance=provenance)


@dataclass(frozen=True)
class FactTemporal:
    asserted: TimePoint
    applies_start: TimePoint
    applies_end: TimePoint
    evidence: TimePoint

    def __post_init__(self):
        if not self.asserted.known or self.asserted.provenance not in ("producer_clock", "owner_edit"):
            raise ValueError("an assertion time is always known and comes from the asserting clock")
        if self.asserted.precision != "instant" or self.asserted.basis != "utc":
            raise ValueError("an assertion time is an explicit UTC instant")

    def to_json(self) -> str:
        return _dumps({"version": FACT_TEMPORAL_VERSION, "asserted": self.asserted.to_json(),
                       "applies": {"start": self.applies_start.to_json(), "end": self.applies_end.to_json()},
                       "evidence": self.evidence.to_json()})

    @classmethod
    def from_json(cls, raw: str) -> "FactTemporal":
        value = json.loads(raw) if type(raw) is str else None
        if (type(value) is not dict or set(value) != {"version", "asserted", "applies", "evidence"}
                or value["version"] != FACT_TEMPORAL_VERSION or type(value["applies"]) is not dict
                or set(value["applies"]) != {"start", "end"}):
            raise ValueError("fact temporal record shape")
        record = cls(TimePoint.from_json(value["asserted"]), TimePoint.from_json(value["applies"]["start"]),
                     TimePoint.from_json(value["applies"]["end"]), TimePoint.from_json(value["evidence"]))
        if record.to_json() != raw:
            raise ValueError("fact temporal record is not canonical")
        return record


def fact_temporal(*, asserted: Optional[TimePoint] = None, applies_start: Optional[TimePoint] = None,
                  applies_end: Optional[TimePoint] = None, evidence: Optional[TimePoint] = None) -> FactTemporal:
    """A record in which every slot the caller did not supply is explicitly unknown."""
    return FactTemporal(asserted or producer_clock(), applies_start or unknown(), applies_end or unknown(),
                        evidence or unknown())


def read_fact_temporal(raw) -> Optional[FactTemporal]:
    """The stored record, or ``None`` for a row written before records existed.

    A present but malformed record is never silently treated as absent: that
    would let a damaged row compare as if it had no evidence time.
    """
    if raw is None:
        return None
    return FactTemporal.from_json(raw)


@dataclass(frozen=True)
class EventTime:
    event: TimePoint

    def to_json(self) -> str:
        return _dumps({"version": EVENT_TIME_VERSION, "event": self.event.to_json()})

    @classmethod
    def from_json(cls, raw: str) -> "EventTime":
        value = json.loads(raw) if type(raw) is str else None
        if type(value) is not dict or set(value) != {"version", "event"} or value["version"] != EVENT_TIME_VERSION:
            raise ValueError("event time record shape")
        record = cls(TimePoint.from_json(value["event"]))
        if record.to_json() != raw:
            raise ValueError("event time record is not canonical")
        return record


def read_event_time(raw) -> Optional[EventTime]:
    if raw is None:
        return None
    return EventTime.from_json(raw)


def event_time(text, *, provenance) -> EventTime:
    return EventTime(parse_point(text, provenance=provenance))


def declared_zone_event_time(naive_text, zone_name) -> Optional[str]:
    """The ``topos-event-time/v1`` record for a naive local reading in a declared IANA zone, or None.

    A source whose owner declared the zone its timestamps are written in (``time_zone`` on the
    source definition) lets the door say when a row happened instead of only which day. The
    record keeps the local reading and adds the zone's offset at that moment, so the instant is
    exact and the rule is auditable from the row. None, never a guess, when the text is not a
    naive instant, the zone is unknown to this machine, or the local time is ambiguous or does
    not exist there (the hour a clock change repeats or skips).
    """
    from datetime import datetime, timedelta

    from .points import _INSTANT

    point = parse_point(naive_text, provenance="unverified_producer")
    if point.precision != "instant" or point.basis != "unrecorded" or type(zone_name) is not str:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(zone_name)
    except Exception:  # noqa: BLE001 -- an unknown name or a machine with no zone database
        return None
    year, month, day, hour, minute, second = (int(group) for group in _INSTANT.fullmatch(naive_text).groups()[:6])
    try:
        local = datetime(year, month, day, hour, minute, second)
        offsets = {local.replace(tzinfo=zone, fold=fold).utcoffset() for fold in (0, 1)}
    except (ValueError, OverflowError):
        return None
    if len(offsets) != 1 or None in offsets:
        return None
    # One offset under both folds: a repeated hour has two, and so does a skipped one (before and
    # after the change), so neither reaches here.
    offset = offsets.pop()
    minutes = int(offset / timedelta(minutes=1))
    if offset != timedelta(minutes=minutes):
        return None
    sign = "+" if minutes >= 0 else "-"
    suffix = "Z" if minutes == 0 else f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    stated = event_time(naive_text + suffix, provenance="unverified_producer")
    return stated.to_json() if stated.event.known else None


def evidence_from_row(row: dict) -> TimePoint:
    """A source row's event time as fact evidence, keeping the row's own provenance.

    A row with an ``event_time_json`` record contributes that record's point.
    A row without one contributes its ``event_at`` text, marked
    ``unverified_producer``: nothing about such a row says which clock wrote
    it, and a legacy writer may have substituted ingestion time.
    """
    raw = row.get("event_time_json")
    if raw is not None:
        return EventTime.from_json(raw).event
    return parse_point(row.get("event_at"), provenance="unverified_producer")
