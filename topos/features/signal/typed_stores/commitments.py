"""Commitment entities for the time dimension (PLAN_TIME_SIGNAL_UPGRADE follow-up).

A Commitment is a recurring obligation that consumes capacity — the standing
structure of someone's week, as opposed to individual busy blocks. v1 derives
them from recurring busy calendar rows: instances sharing a (weekday,
start-time, duration) signature form one series. Payloads carry bands and
counts only — never titles or attendees.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..signal_object_store import SignalObjectStore

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MIN_SERIES_OCCURRENCES = 2


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _movability_band(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 0.7:
        return "flexible"
    if score >= 0.4:
        return "negotiable"
    return "fixed"


def recompute_commitments(store: SignalObjectStore, conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "calendar_events"):
        return 0
    for required in ("is_recurring", "is_busy"):
        if not _table_has_column(conn, "calendar_events", required):
            return 0
    has_attendees = _table_has_column(conn, "calendar_events", "attendee_count")
    has_movability = _table_has_column(conn, "calendar_events", "movability_score")
    cols = ["event_id", "starts_at", "ends_at"]
    if has_attendees:
        cols.append("attendee_count")
    if has_movability:
        cols.append("movability_score")
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM calendar_events "
        "WHERE is_recurring=1 AND is_busy=1 ORDER BY starts_at LIMIT 2000"
    ).fetchall()

    series: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        data = dict(zip(cols, row))
        start = _parse_iso(data.get("starts_at"))
        end = _parse_iso(data.get("ends_at"))
        if start is None or end is None or end <= start:
            continue
        if (start.tzinfo is None) != (end.tzinfo is None):
            continue
        duration_min = int((end - start).total_seconds() // 60)
        key = (_DAY_NAMES[start.weekday()], start.strftime("%H:%M"), duration_min)
        data["_start"] = start
        data["_end"] = end
        series[key].append(data)

    created = 0
    for (day, clock, duration_min), instances in series.items():
        if len(instances) < _MIN_SERIES_OCCURRENCES:
            continue
        first = min(i["_start"] for i in instances)
        last = max(i["_end"] for i in instances)
        span_weeks = max(1.0, ((last - first).days + 1) / 7.0)
        load_weight = round(len(instances) / span_weeks * duration_min / 60.0, 2)
        attendee_counts = [
            int(i.get("attendee_count") or 0) for i in instances if has_attendees
        ]
        group = bool(attendee_counts) and max(attendee_counts) > 1
        movabilities = [
            float(i["movability_score"])
            for i in instances
            if has_movability and i.get("movability_score") is not None
        ]
        avg_movability = (
            sum(movabilities) / len(movabilities) if movabilities else None
        )
        confidence = round(min(0.9, 0.5 + 0.1 * len(instances)), 3)
        store.upsert_object(
            "time",
            "Commitment",
            f"{day}:{clock}:{duration_min}",
            {
                "kind": "recurring_meeting" if group else "recurring_block",
                "recurrence": f"weekly:{day}",
                "day_of_week": day,
                "start_clock": clock,
                "duration_minutes": duration_min,
                "occurrence_count": len(instances),
                "load_weight": load_weight,
                "movability_band": _movability_band(avg_movability),
                "starts_at": first.isoformat(),
                "ends_at": last.isoformat(),
                "confidence": confidence,
            },
            source_refs=[
                {"table": "calendar_events", "id": str(i.get("event_id"))}
                for i in instances[:10]
            ],
            confidence=confidence,
            extractor_version="commitments_v1",
        )
        created += 1
    return created
