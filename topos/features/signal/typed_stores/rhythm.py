"""Behavioral rhythm layer for the time dimension (PLAN_TIME_SIGNAL_UPGRADE M2).

Derives when the person is active and *what kind of active* from canonical
behavior timestamps — the piece of the time signal a calendar can't provide.
Produces the RoutinePattern entities and routine_confidence aggregate declared
in definitions/time.json.

v1 limitation: hours are read from each timestamp as stored; no cross-timezone
normalization. Payloads never contain titles, URLs, or content — only counts.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..signal_object_store import SignalObjectStore

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_BANDS = ("morning", "afternoon", "evening", "night")
_CELL_COUNT = len(_DAY_NAMES) * len(_TIME_BANDS)
_MIN_CELL_OBSERVATIONS = 3

# (table, timestamp column, activity kind, extra WHERE clause)
_BEHAVIOR_SOURCES: Tuple[Tuple[str, str, str, str], ...] = (
    ("conversation_messages", "event_at", "communication", ""),
    ("activity_events", "occurred_at", "browsing", ""),
    ("journal_entries", "entry_at", "logged", ""),
    ("calendar_events", "starts_at", "scheduled", "WHERE is_busy=1"),
)


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


def _time_band(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _collect_observations(
    conn: sqlite3.Connection,
) -> Tuple[Dict[Tuple[str, str], Dict[str, int]], int, int]:
    """Cell → per-kind counts, total observations, span in days."""
    cells: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    for table, ts_col, kind, where in _BEHAVIOR_SOURCES:
        if not _table_exists(conn, table) or not _table_has_column(conn, table, ts_col):
            continue
        clause = where if (not where or _table_has_column(conn, table, "is_busy")) else ""
        rows = conn.execute(
            f"SELECT {ts_col} FROM {table} {clause} ORDER BY {ts_col} DESC LIMIT 2000"
        ).fetchall()
        for (raw,) in rows:
            ts = _parse_iso(raw)
            if ts is None:
                continue
            cells[(_DAY_NAMES[ts.weekday()], _time_band(ts.hour))][kind] += 1
            total += 1
            naive = ts.replace(tzinfo=None)
            earliest = naive if earliest is None or naive < earliest else earliest
            latest = naive if latest is None or naive > latest else latest
    span_days = max(1, (latest - earliest).days + 1) if earliest and latest else 0
    return cells, total, span_days


def recompute_rhythm(store: SignalObjectStore, conn: sqlite3.Connection) -> int:
    cells, total, span_days = _collect_observations(conn)
    if total == 0:
        return 0

    created = 0
    uniform_share = 1.0 / _CELL_COUNT
    active_bands: List[Dict[str, Any]] = []
    for (day, band), kinds in cells.items():
        frequency = sum(kinds.values())
        share = frequency / total
        if frequency < _MIN_CELL_OBSERVATIONS or share <= uniform_share:
            continue
        dominant_kind = max(kinds.items(), key=lambda kv: kv[1])[0]
        # More observations in a cell → more trust, saturating around ~30.
        confidence = round(min(0.95, 0.4 + 0.1 * math.log10(max(frequency, 1)) * 3), 3)
        payload = {
            "day_of_week": day,
            "time_band": band,
            "frequency": frequency,
            "share": round(share, 4),
            "dominant_kind": dominant_kind,
            "kind_counts": dict(kinds),
            "confidence": confidence,
        }
        store.upsert_object(
            "time",
            "RoutinePattern",
            f"{day}:{band}",
            payload,
            confidence=confidence,
            extractor_version="rhythm_v1",
        )
        active_bands.append(payload)
        created += 1

    counts = [sum(kinds.values()) for kinds in cells.values()]
    entropy = -sum(
        (c / total) * math.log(c / total) for c in counts if c > 0
    )
    max_entropy = math.log(_CELL_COUNT)
    concentration = round(1.0 - (entropy / max_entropy if max_entropy else 0.0), 4)
    coverage = round(len(cells) / _CELL_COUNT, 4)
    # Predictability: enough data, over enough days, with a real pattern.
    sample_factor = min(1.0, total / 200.0)
    span_factor = min(1.0, span_days / 14.0)
    confidence = round(
        min(0.95, sample_factor * span_factor * (0.5 + 0.5 * concentration)), 3
    )
    active_bands.sort(key=lambda b: int(b["frequency"]), reverse=True)
    store.upsert_object(
        "time",
        "routine_confidence",
        "rolling",
        {
            "sample_count": total,
            "span_days": span_days,
            "coverage": coverage,
            "concentration": concentration,
            "confidence": confidence,
            "top_bands": [
                {
                    "day_of_week": b["day_of_week"],
                    "time_band": b["time_band"],
                    "dominant_kind": b["dominant_kind"],
                    "share": b["share"],
                }
                for b in active_bands[:5]
            ],
        },
        confidence=confidence,
        extractor_version="rhythm_v1",
    )
    return created + 1
