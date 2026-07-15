"""Google Calendar canonical mapper (PLAN_CANONICAL_CALENDAR_DOCUMENTS Part B).

Reads a raw Google Calendar API ``events`` resource dict directly (the
ingestion parser is a near-identity passthrough — see
``ingestion/parsers/google_calendar_parser.py``) and populates two layers on
``calendar_events``:

- **Availability layer** (raw fields, deterministic): is_busy, is_all_day,
  self_response_status, is_organizer, is_recurring, plus straight passthrough
  fields (event_type, timezone, location, description, url, attendee/accepted
  counts, created_at/updated_at). These make free/busy and availability
  queries filterable in SQL.
- **Value layer** (derived, explainable v1 heuristics): attendance_priority,
  movability_score, value_score, value_reason, priority_confidence. Computed
  at map time from the raw event — no enrichment round trip required for v1
  (a later LLM refinement pass can UPDATE these and raise
  priority_confidence; noted as a follow-up, not built here).

Two numeric judgment calls the spec left unpinned (documented here so they're
easy to retune):
- ``_SMALL_MEETING_MAX_ATTENDEES`` — the small/large meeting-size threshold
  used by the movability heuristic.
- the "guest + small meeting" movability bucket, which the spec enumerated
  three of four role/size buckets for (organizer+small, organizer+large,
  guest+large) but not this one; ``_MOVABILITY_GUEST_SMALL`` fills the gap
  with an interpolated value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata

# Attendance priority labels.
_SKIP = "skip"
_MUST_ATTEND = "must_attend"
_ATTEND = "attend"
_OPTIONAL = "optional"

# Movability role/size buckets (spec §B3 pins three of four; see module
# docstring for the fourth).
_SMALL_MEETING_MAX_ATTENDEES = 4
_MOVABILITY_SOLO = 0.7
_MOVABILITY_ORGANIZER_SMALL = 0.8
_MOVABILITY_ORGANIZER_LARGE = 0.4
_MOVABILITY_GUEST_LARGE = 0.15
_MOVABILITY_GUEST_SMALL = 0.5  # not pinned by spec — documented judgment call
_MOVABILITY_RECURRING_BONUS = 0.2

_VALUE_BASELINE: Dict[str, float] = {
    _MUST_ATTEND: 0.8,
    _ATTEND: 0.5,
    _OPTIONAL: 0.3,
    _SKIP: 0.1,
}
_HIGH_VALUE_KEYWORDS = ("interview", "flight", "deadline", "appointment", "court")
_LOW_VALUE_KEYWORDS = ("standup", "sync", "hold", "block")

_PRIORITY_CONFIDENCE_HEURISTIC = 0.5


def _is_busy(status: Optional[str], transparency: Optional[str]) -> bool:
    """opaque (default) = busy; transparent or cancelled = not busy."""
    return (
        str(transparency or "").strip().lower() != "transparent"
        and str(status or "").strip().lower() != "cancelled"
    )


def _is_all_day(start: Dict[str, Any]) -> bool:
    return bool(str(start.get("date") or "").strip()) and not str(
        start.get("dateTime") or ""
    ).strip()


def _self_response_status(attendees: List[Dict[str, Any]]) -> Optional[str]:
    for attendee in attendees:
        if isinstance(attendee, dict) and attendee.get("self") is True:
            return attendee.get("responseStatus")
    return None


def _event_time(node: Dict[str, Any]) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    return node.get("dateTime") or node.get("date")


def _attendance_priority(
    *,
    self_response: Optional[str],
    transparency: Optional[str],
    status: Optional[str],
    is_organizer_flag: bool,
    attendee_count: int,
    is_all_day_flag: bool,
) -> str:
    response = str(self_response or "").strip().lower()
    declined = response == "declined"
    free_transparency = str(transparency or "").strip().lower() == "transparent"
    cancelled = str(status or "").strip().lower() == "cancelled"
    if declined or free_transparency or cancelled:
        return _SKIP
    if is_organizer_flag and attendee_count > 1:
        return _MUST_ATTEND
    if response == "accepted" and attendee_count > 1:
        return _ATTEND
    if response in ("needsaction", "tentative"):
        return _OPTIONAL
    if is_all_day_flag and attendee_count <= 1:
        return _OPTIONAL
    return _ATTEND


def _movability_score(
    *,
    priority: str,
    is_organizer_flag: bool,
    attendee_count: int,
    is_recurring_flag: bool,
) -> float:
    if priority == _SKIP:
        # Nothing to defend — a skipped/cancelled/declined slot is fully movable.
        return 1.0
    if attendee_count <= 1:
        base = _MOVABILITY_SOLO
    elif is_organizer_flag:
        base = (
            _MOVABILITY_ORGANIZER_SMALL
            if attendee_count <= _SMALL_MEETING_MAX_ATTENDEES
            else _MOVABILITY_ORGANIZER_LARGE
        )
    else:
        base = (
            _MOVABILITY_GUEST_SMALL
            if attendee_count <= _SMALL_MEETING_MAX_ATTENDEES
            else _MOVABILITY_GUEST_LARGE
        )
    if is_recurring_flag:
        base += _MOVABILITY_RECURRING_BONUS
    return max(0.0, min(1.0, base))


def _value_score(priority: str, title: Optional[str]) -> float:
    score = _VALUE_BASELINE.get(priority, _VALUE_BASELINE[_OPTIONAL])
    lowered = str(title or "").lower()
    if any(keyword in lowered for keyword in _HIGH_VALUE_KEYWORDS):
        score += 0.2
    if any(keyword in lowered for keyword in _LOW_VALUE_KEYWORDS):
        score -= 0.1
    return max(0.0, min(1.0, score))


def _value_reason(
    *,
    priority: str,
    self_response: Optional[str],
    transparency: Optional[str],
    status: Optional[str],
    is_all_day_flag: bool,
    attendee_count: int,
) -> str:
    response = str(self_response or "").strip().lower()
    if response == "declined":
        return "you declined this event"
    if str(status or "").strip().lower() == "cancelled":
        return "event was cancelled"
    if str(transparency or "").strip().lower() == "transparent":
        return "marked as free time (transparent)"
    if priority == _MUST_ATTEND:
        guests = max(attendee_count - 1, 0)
        return f"you organize this with {guests} guest{'s' if guests != 1 else ''}"
    if priority == _ATTEND:
        return f"you accepted, {attendee_count} attendees"
    if priority == _OPTIONAL:
        if response in ("needsaction", "tentative"):
            return "you haven't responded yet"
        if is_all_day_flag:
            return "all-day personal block"
        return "optional attendance"
    return "regular event"


@dataclass
class GoogleCalendarMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        google_event_id = str(p.get("id") or normalized.record_id)
        calendar_id = str(p.get("calendar_id") or "primary")
        event_id = f"gcal:{calendar_id}:{google_event_id}"

        start = p.get("start") if isinstance(p.get("start"), dict) else {}
        end = p.get("end") if isinstance(p.get("end"), dict) else {}
        status = p.get("status")
        transparency = p.get("transparency")
        organizer = p.get("organizer") if isinstance(p.get("organizer"), dict) else {}
        attendees_raw = p.get("attendees")
        attendees = attendees_raw if isinstance(attendees_raw, list) else []

        is_busy = _is_busy(status, transparency)
        is_all_day_flag = _is_all_day(start)
        self_response_status = _self_response_status(attendees)
        is_organizer_flag = organizer.get("self") is True
        is_recurring_flag = bool(str(p.get("recurringEventId") or "").strip())
        attendee_count = len(attendees)
        accepted_count = sum(
            1
            for attendee in attendees
            if isinstance(attendee, dict)
            and str(attendee.get("responseStatus") or "").strip().lower() == "accepted"
        )
        title = p.get("summary") or p.get("title")

        priority = _attendance_priority(
            self_response=self_response_status,
            transparency=transparency,
            status=status,
            is_organizer_flag=is_organizer_flag,
            attendee_count=attendee_count,
            is_all_day_flag=is_all_day_flag,
        )
        movability = _movability_score(
            priority=priority,
            is_organizer_flag=is_organizer_flag,
            attendee_count=attendee_count,
            is_recurring_flag=is_recurring_flag,
        )
        value = _value_score(priority, title)
        reason = _value_reason(
            priority=priority,
            self_response=self_response_status,
            transparency=transparency,
            status=status,
            is_all_day_flag=is_all_day_flag,
            attendee_count=attendee_count,
        )

        metadata = {
            "calendar_id": calendar_id,
            "creator": p.get("creator"),
        }
        canonical = {
            "event_id": event_id,
            "title": title,
            "starts_at": _event_time(start),
            "ends_at": _event_time(end),
            "is_busy": is_busy,
            "status": status,
            "is_all_day": is_all_day_flag,
            "self_response_status": self_response_status,
            "is_organizer": is_organizer_flag,
            "is_recurring": is_recurring_flag,
            "event_type": p.get("eventType"),
            "timezone": start.get("timeZone") or end.get("timeZone"),
            "location": p.get("location"),
            "description": p.get("description"),
            "url": p.get("htmlLink"),
            "attendee_count": attendee_count,
            "accepted_count": accepted_count,
            "created_at": p.get("created"),
            "updated_at": p.get("updated"),
            "attendance_priority": priority,
            "movability_score": movability,
            "value_score": value,
            "value_reason": reason,
            "priority_confidence": _PRIORITY_CONFIDENCE_HEURISTIC,
            "source_record_id": google_event_id,
            "metadata_json": {k: v for k, v in metadata.items() if v is not None},
        }
        return CanonicalRecord(record_id=event_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="google_calendar", mapping_version=self.version)
