"""Journal time-log field normalization for bundled journal.time_log.v1 parser."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def parse_time_log_timestamp(date_str: str, time_str: str) -> str:
    date_part = str(date_str or "").strip()
    time_part = str(time_str or "").strip()
    if not date_part:
        return ""
    combined = f"{date_part} {time_part}".strip()
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(combined, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return combined.replace(" ", "T") if time_part else date_part


def normalize_time_log_datetime(value: Any) -> str:
    """Normalize a single datetime field to YYYY-MM-DDTHH:MM:SS when possible."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "T" in text:
        normalized = text.replace("Z", "+00:00")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                parsed = datetime.strptime(normalized, fmt)
                return parsed.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
        return text.replace("Z", "").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return text


def resolve_time_log_entry_at(payload: Dict[str, Any]) -> str:
    """When the entry was recorded/submitted (distinct from session start)."""
    for key in ("entry_at", "created_at", "createdAt", "submitted_at", "recorded_at", "timestamp"):
        direct = normalize_time_log_datetime(payload.get(key))
        if direct:
            return direct
    return ""


def resolve_time_log_start_at(payload: Dict[str, Any]) -> str:
    for key in ("starts_at", "start_at"):
        direct = normalize_time_log_datetime(payload.get(key))
        if direct:
            return direct
    return parse_time_log_timestamp(str(payload.get("startDate") or ""), str(payload.get("startTime") or ""))


def resolve_time_log_end_at(payload: Dict[str, Any]) -> str:
    direct = normalize_time_log_datetime(payload.get("ends_at"))
    if direct:
        return direct
    return parse_time_log_timestamp(str(payload.get("endDate") or ""), str(payload.get("endTime") or ""))


def time_log_payload_has_start_time(payload: Dict[str, Any]) -> bool:
    return bool(resolve_time_log_start_at(payload))


def build_time_log_content(payload: Dict[str, Any]) -> str:
    """Canonical journal content: goal and accomplished text only."""
    parts: List[str] = []
    goal = str(payload.get("goal") or "").strip()
    if goal:
        parts.append(f"Goal: {goal}")
    accomplished = str(payload.get("accomplished") or "").strip()
    if accomplished:
        parts.append(f"Accomplished: {accomplished}")
    return "\n\n".join(parts)


def normalize_time_log_payload(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    num = str(payload.get("num") or record_id).strip()
    entry_id = f"tl-{num}" if num else f"tl-{record_id}"
    entry_at = resolve_time_log_entry_at(payload)
    starts_at = resolve_time_log_start_at(payload)
    ends_at = resolve_time_log_end_at(payload)
    emotion = str(payload.get("emotionLabel") or "").strip()
    if emotion.lower() == "null":
        emotion = ""
    goal = str(payload.get("goal") or "").strip()
    accomplished = str(payload.get("accomplished") or "").strip()
    project = str(payload.get("project") or "").strip()
    duration_raw = payload.get("duration")
    duration = str(duration_raw).strip() if duration_raw is not None and str(duration_raw).strip() else None
    location = str(payload.get("location") or "").strip() or None
    group = str(payload.get("group") or "").strip() or None
    return {
        "entry_id": entry_id,
        "record_id": entry_id,
        "dataset_id": dataset_id,
        "entry_at": entry_at or None,
        "starts_at": starts_at or None,
        "ends_at": ends_at or None,
        "mood_tag": emotion or None,
        "category": project or None,
        "project": project or None,
        "goal": goal or None,
        "accomplished": accomplished or None,
        "content": build_time_log_content(payload),
        "duration": duration,
        "duration_minutes": duration,
        "place_name": location,
        "people": group,
        "goal_entities": str(payload.get("goalEntities") or "").strip() or None,
        "accomplished_entities": str(payload.get("accomplishedEntities") or "").strip() or None,
        "completed": _coerce_bool(payload.get("completed")),
        "location": location,
        "group": group,
        "_metadata": {
            "session_num": num,
            "goal_word_count": payload.get("goalWordCount"),
            "accomplished_word_count": payload.get("accomplishedWordCount"),
            "has_form": payload.get("hasForm"),
            "accomplished": accomplished or None,
            "duration_minutes": duration,
        },
    }
