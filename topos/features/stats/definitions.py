"""Seed statistic definitions + row value/group extractors.

Definitions live in code (source of truth) and are mirrored into
stat_definitions so the UI/API can enumerate and toggle them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..provenance.roles import owner_authored
from ..provenance.posture import make_posture_resolver
from .fold import parse_ts

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Lazily-built posture resolver (P1.4): the sent-family / authored-rhythm gates
# must honour the owner's per-connector posture override so an 'ambient'-flagged
# connector's rows never count toward "how many messages have I sent" — even if
# a row is is_from_self. Cached per source_id inside the resolver.
_POSTURE_RESOLVER = None


def _row_posture(row: Dict[str, Any]) -> str:
    stamped = row.get("_posture")
    if stamped:
        return str(stamped).strip().lower()
    global _POSTURE_RESOLVER
    if _POSTURE_RESOLVER is None:
        _POSTURE_RESOLVER = make_posture_resolver()
    try:
        return _POSTURE_RESOLVER(row)
    except Exception:  # noqa: BLE001
        return "mixed"


def _row_owner_authored(row: Dict[str, Any], default_table: str) -> bool:
    """Owner-authored gate with the P1.4 posture override applied."""
    return owner_authored(
        row,
        table=_row_table(row, default_table),
        posture=_row_posture(row),
    )


def _row_table(row: Dict[str, Any], default: str = "") -> str:
    """Table context for the authored gate; producers stamp _table (P0.1),
    and each gated group_by/value_expr is bound to exactly one canonical
    table, so its table is a safe fallback for unstamped rows."""
    return str(row.get("_table") or row.get("canonical_table") or default)


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
    if group_by == "city":
        value = row.get("city")
        return str(value).strip() if value else None
    if group_by == "place_name":
        value = row.get("place_name")
        return str(value).strip() if value else None
    if group_by == "mood_tag":
        value = row.get("mood_tag")
        return str(value).strip().lower() if value else None
    if group_by == "title":
        value = str(row.get("title") or "").strip()
        return value[:60] if value else None
    if group_by == "contact_id":
        value = row.get("sender_id") or row.get("contact_id") or row.get("from")
        return str(value).strip() if value else None
    if group_by == "conversation_id":
        value = row.get("conversation_id") or row.get("thread_id")
        return str(value).strip() if value else None
    if group_by == "cluster_id":
        value = row.get("cluster_id")
        return str(value).strip() if value else None
    # ---- authored-gated group keys (PLAN_PROVENANCE_SPLIT P1.3) ----------
    # Returning None makes the engine skip the row entirely: the fold gate
    # lives HERE because stat_definitions has no filter column and the fold
    # loop consults group_key_for/histogram_value for every row.
    if group_by == "authored_total":
        # Single-group family ("self", non-empty so insights render it):
        # counts ONLY rows the owner sent — the honest denominator for
        # "how many messages have I sent" (IMB6's needle). P1.4: an
        # ambient-flagged connector's rows are capped below authored and
        # excluded here.
        if _row_owner_authored(row, "conversation_messages"):
            return "self"
        return None
    if group_by == "authored_conversation":
        if not _row_owner_authored(row, "conversation_messages"):
            return None
        value = row.get("conversation_id") or row.get("thread_id")
        return str(value).strip() if value else None
    if group_by == "hour_of_week_authored":
        # Owner rhythm from owner rows: assistant/system rows must not shape
        # "when is this person active".
        if not _row_owner_authored(row, "ai_chat_messages"):
            return None
        return f"{_WEEKDAYS[ts.weekday()]} {ts.hour:02d}:00" if ts else None
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
        "windows_json": '["all", "d30", "d90"]',
    },
    {
        # P1.3: authored-only fold gate — the owner's chat rhythm comes from
        # rows the owner typed; assistant replies land seconds later and were
        # doubling every band. (Historical stat_state is not re-minted here;
        # backfill is the plan's deferred re-mint item.)
        "stat_id": "ai_chat.hour_of_week",
        "canonical_table": "ai_chat_messages",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "hour_of_week_authored",
        "dimension": "work",
        "windows_json": '["all", "d30", "d90"]',
    },
    # Wellbeing: session durations and category mix from journals
    {
        "stat_id": "journal.duration.by_category",
        "canonical_table": "journal_entries",
        "group_by": "category",
        "stat_kind": "mean_var",
        "value_expr": "duration_minutes",
        "dimension": "wellbeing",
        "windows_json": '["all", "d30", "d90"]',
    },
    {
        "stat_id": "journal.category.mix",
        "canonical_table": "journal_entries",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "category",
        "dimension": "wellbeing",
        "windows_json": '["all", "d30", "d90"]',
    },
    # Relationships: OWNER-SENT message volume (PLAN_PROVENANCE_SPLIT P1.3).
    # The 'messages.volume.sent.*' family id prefix is the query-side selection
    # contract for first-person volume asks ("how many messages have I sent"):
    # sent-counts answer them; thread/contact volume is exposure, not the
    # owner's expression (IMB6's 23-vs-2000 poison pair).
    {
        "stat_id": "messages.volume.sent.total",
        "canonical_table": "conversation_messages",
        "group_by": "authored_total",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "relationships",
        "windows_json": '["all", "d7", "d30", "d90"]',
    },
    {
        "stat_id": "messages.volume.sent.by_thread",
        "canonical_table": "conversation_messages",
        "group_by": "authored_conversation",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "relationships",
        "windows_json": '["all", "d7", "d30", "d90"]',
    },
    # Relationships: message volume and cadence per contact
    {
        "stat_id": "messages.volume.by_contact",
        "canonical_table": "conversation_messages",
        "group_by": "contact_id",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "relationships",
        "windows_json": '["all", "d7", "d30", "d90"]',
    },
    {
        "stat_id": "messages.cadence.by_contact",
        "canonical_table": "conversation_messages",
        "group_by": "contact_id",
        "stat_kind": "intervals",
        "value_expr": "one",
        "dimension": "relationships",
        "windows_json": '["all", "d30", "d90"]',
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
        "windows_json": '["all", "d30", "d90"]',
    },
    # Places: visit frequency by city and by place — "which cities/places do I
    # go" is a frequency question a recency-ordered browse can't answer.
    {
        "stat_id": "places.visits.by_city",
        "canonical_table": "location_events",
        "group_by": "city",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "places",
        "windows_json": '["all", "d30", "d90"]',
    },
    {
        "stat_id": "places.visits.by_place",
        "canonical_table": "location_events",
        "group_by": "place_name",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "places",
        "windows_json": '["all", "d30", "d90"]',
    },
    # Wellbeing: mood mix ("what moods do I record most often").
    {
        "stat_id": "journal.mood.mix",
        "canonical_table": "journal_entries",
        "group_by": "none",
        "stat_kind": "histogram",
        "value_expr": "mood_tag",
        "dimension": "wellbeing",
        "windows_json": '["all", "d30", "d90"]',
    },
    # Interests: most-visited pages ("what do I browse the most").
    {
        "stat_id": "activity.visits.by_title",
        "canonical_table": "activity_events",
        "group_by": "title",
        "stat_kind": "count",
        "value_expr": "one",
        "dimension": "interests",
        "windows_json": '["all", "d30", "d90"]',
    },
]

# Code-side stat payload markers (no stat_definitions column needed; attached
# by load_enabled_definitions and carried into the promoted insight's
# stat_summary — see insights.render_insights). Contract (PLAN_PROVENANCE_SPLIT
# P1.3 / contract 5): exposure-profile stats carry {"ledger": "exposure"} so
# query-side stat selection can prefer expression over exposure for
# first-person asks.
STAT_PAYLOADS: Dict[str, Dict[str, Any]] = {
    # Browsing titles are what the owner was EXPOSED to, not what they said —
    # demoted from evidence-of-interest to a labeled exposure profile.
    "activity.visits.by_title": {"ledger": "exposure"},
}

# Histogram value extractors keyed off value_expr when kind == histogram
HISTOGRAM_VALUE_KEYS = {
    "hour_of_week",
    "hour_of_week_authored",
    "hour_of_day",
    "weekday",
    "category",
    "mood_tag",
}


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
                decay_half_life_days=excluded.decay_half_life_days,
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
        defn = {
            "stat_id": row[0],
            "canonical_table": row[1],
            "group_by": row[2],
            "stat_kind": row[3],
            "value_expr": row[4],
            "dimension": row[5],
            "decay_half_life_days": row[6],
            "windows": json.loads(row[7] or '["all"]'),
        }
        payload = STAT_PAYLOADS.get(str(row[0]))
        if payload:
            defn["payload"] = dict(payload)
        out.append(defn)
    return out
