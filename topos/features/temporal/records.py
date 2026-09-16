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


def producer_clock(now: Optional[datetime] = None) -> TimePoint:
    """The asserting producer's own clock as an explicit-UTC instant."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("producer clock must be timezone-aware")
    text = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parse_point(text, provenance="producer_clock")


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
