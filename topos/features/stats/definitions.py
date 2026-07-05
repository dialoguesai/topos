"""Seed statistic definitions + row value/group extractors.

Definitions live in code (source of truth) and are mirrored into
stat_definitions so the UI/API can enumerate and toggle them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .fold import parse_ts

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _row_meta(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return meta if isinstance(meta, dict) else {}


def row_event_ts(row: Dict[str, Any]) -> Optional[datetime]:
    for field in ("event_at", "ts", "entry_at", "occurred_at", "starts_at", "created_at"):
        ts = parse_ts(row.get(field))
        if ts is not None:
            return ts
    return None


def _duration_minutes(row: Dict[str, Any]) -> Optional[float]:
    for source in (row, _row_meta(row)):
        value = source.get("duration_minutes")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    starts, ends = parse_ts(row.get("starts_at")), parse_ts(row.get("ends_at"))
    if starts and ends and ends > starts:
        return (ends - starts).total_seconds() / 60.0
    return None


def _amount(row: Dict[str, Any]) -> Optional[float]:
    value = row.get("amount")
    try:
        return abs(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


VALUE_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Optional[float]]] = {
    "one": lambda row: 1.0,
    "duration_minutes": _duration_minutes,
    "amount": _amount,
}


def group_key_for(row: Dict[str, Any], group_by: str) -> Optional[str]:
    if group_by == "none":
        return ""
    ts = row_event_ts(row)
    if group_by == "hour_of_day":
        return f"{ts.hour:02d}" if ts else None
    if group_by == "weekday":
        return _WEEKDAYS[ts.weekday()] if ts else None
    if group_by == "hour_of_week":
        return f"{_WEEKDAYS[ts.weekday()]} {ts.hour:02d}:00" if ts else None
    if group_by == "category":
        value = row.get("category") or row.get("url_category") or _row_meta(row).get("category")
        return str(value).strip().lower() if value else None
    if group_by == "contact_id":
        value = row.get("sender_id") or row.get("contact_id") or row.get("from")
        return str(value).strip() if value else None
    if group_by == "conversation_id":
        value = row.get("conversation_id") or row.get("thread_id")
        return str(value).strip() if value else None
    if group_by == "cluster_id":
        value = row.get("cluster_id")
        return str(value).strip() if value else None
    return None


# stat_id, canonical_table, group_by, kind, value_expr, dimension, half_life_days, windows
SEED_DEFINITIONS: List[Dict[str, Any]] = [
    # Rhythm: when is this person active, per source type
    {
        "stat_id": "activity.hour_of_week",
        "canonical_table": "activity_events",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "hour_of_week",
        "dimension": "interests",
    },
    {
        "stat_id": "ai_chat.hour_of_week",
        "canonical_table": "ai_chat_messages",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "hour_of_week",
        "dimension": "work",
    },
    # Wellbeing: session durations and category mix from journals
    {
        "stat_id": "journal.duration.by_category",
        "canonical_table": "journal_entries",
        "group_by": "category",
        "stat_kind": "mean_var",
        "value_expr": "duration_minutes",
        "dimension": "wellbeing",
    },
    {
        "stat_id": "journal.category.mix",
        "canonical_table": "journal_entries",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "category",
        "dimension": "wellbeing",
    },
    # Relationships: message volume and cadence per contact
    {
        "stat_id": "messages.volume.by_contact",
        "canonical_table": "conversation_messages",
        "group_by": "contact_id",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "relationships",
    },
    {
        "stat_id": "messages.cadence.by_contact",
        "canonical_table": "conversation_messages",
        "group_by": "contact_id",
        "stat_kind": "intervals",
        "value_expr": "one",
        "dimension": "relationships",
    },
    # Resources: spend by category (daily buckets enable window trends)
    {
        "stat_id": "financial.spend.by_category",
        "canonical_table": "financial_transactions",
        "group_by": "category",
        "stat_kind": "sum",
        "value_expr": "amount",
        "dimension": "resources",
        "windows_json": '["all", "d30", "d90"]',
    },
    # Time: calendar commitment durations
    {
        "stat_id": "calendar.commitment.duration",
        "canonical_table": "calendar_events",
        "group_by": "none",
        "stat_kind": "mean_var",
        "value_expr": "duration_minutes",
        "dimension": "time",
    },
]

# Histogram value extractors keyed off value_expr when kind == histogram
HISTOGRAM_VALUE_KEYS = {"hour_of_week", "hour_of_day", "weekday", "category"}


def histogram_value(row: Dict[str, Any], value_expr: str) -> Optional[str]:
    return group_key_for(row, value_expr)


def seed_definitions(conn) -> int:
    written = 0
    for defn in SEED_DEFINITIONS:
        conn.execute(
            """
            INSERT INTO stat_definitions (
                stat_id, canonical_table, group_by, stat_kind, value_expr,
                dimension, decay_half_life_days, windows_json, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(stat_id) DO UPDATE SET
                canonical_table=excluded.canonical_table,
                group_by=excluded.group_by,
                stat_kind=excluded.stat_kind,
                value_expr=excluded.value_expr,
                dimension=excluded.dimension,
                windows_json=excluded.windows_json
            """,
            (
                defn["stat_id"],
                defn["canonical_table"],
                defn.get("group_by", "none"),
                defn["stat_kind"],
                defn.get("value_expr"),
                defn.get("dimension", "memory"),
                defn.get("decay_half_life_days"),
                defn.get("windows_json", '["all"]'),
            ),
        )
        written += 1
    conn.commit()
    return written


def load_enabled_definitions(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT stat_id, canonical_table, group_by, stat_kind, value_expr,
               dimension, decay_half_life_days, windows_json
        FROM stat_definitions WHERE enabled = 1
        """
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "stat_id": row[0],
                "canonical_table": row[1],
                "group_by": row[2],
                "stat_kind": row[3],
                "value_expr": row[4],
                "dimension": row[5],
                "decay_half_life_days": row[6],
                "windows": json.loads(row[7] or '["all"]'),
            }
        )
    return out
