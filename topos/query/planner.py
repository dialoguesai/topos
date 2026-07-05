"""Deterministic query planner: one structured parse ahead of retrieval.

Replaces scattered per-scope regex heuristics with a single QueryPlan:
  entities          — alias-table linking against the entity registry (P3)
  time_range        — explicit dates + relative phrases ("last week")
  aggregate_intent  — routes "how often / how much / typically" to stat insights
  dimensions        — keyword-mapped signal dimensions
  semantic_residual — query minus matched entity/time spans (what's left for
                      vector search)

No model call: this runs on every query. TOPOS_QUERY_PLANNER (default on).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


def query_planner_enabled() -> bool:
    return os.environ.get("TOPOS_QUERY_PLANNER", "on").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


@dataclass
class QueryPlan:
    query_text: str
    entities: List[Dict[str, Any]] = field(default_factory=list)
    time_range: Optional[Tuple[str, str]] = None
    aggregate_intent: bool = False
    temporal_shift: Optional[str] = None  # 'past' for "before/prior/used to"
    dimensions: List[str] = field(default_factory=list)
    semantic_residual: str = ""

    def to_meta(self) -> Dict[str, Any]:
        return {
            "entities": [e.get("canonical_name") for e in self.entities],
            "time_range": list(self.time_range) if self.time_range else None,
            "aggregate_intent": self.aggregate_intent,
            "temporal_shift": self.temporal_shift,
            "dimensions": self.dimensions,
        }


_AGGREGATE_RE = re.compile(
    r"\b(how often|how much|how many|how long|average|avg|typically|usually|"
    r"most active|per (?:day|week|month)|trend|total spend|spend on|rhythm|habit)\b",
    re.I,
)

_PAST_RE = re.compile(r"\b(before|prior|previous|previously|used to|former|formerly)\b", re.I)

_RELATIVE_RANGES = (
    (re.compile(r"\btoday\b", re.I), lambda now: (now, now)),
    (re.compile(r"\byesterday\b", re.I), lambda now: (now - timedelta(days=1), now - timedelta(days=1))),
    (re.compile(r"\bthis week\b", re.I), lambda now: (now - timedelta(days=now.weekday()), now)),
    (
        re.compile(r"\blast week\b", re.I),
        lambda now: (
            now - timedelta(days=now.weekday() + 7),
            now - timedelta(days=now.weekday() + 1),
        ),
    ),
    (re.compile(r"\bthis month\b", re.I), lambda now: (now.replace(day=1), now)),
    (
        re.compile(r"\blast month\b", re.I),
        lambda now: (
            (now.replace(day=1) - timedelta(days=1)).replace(day=1),
            now.replace(day=1) - timedelta(days=1),
        ),
    ),
    (re.compile(r"\blast (\d{1,2}) days\b", re.I), None),  # handled specially
)

_DIMENSION_KEYWORDS = {
    "work": ("work", "job", "meeting", "project", "employer", "career", "colleague"),
    "wellbeing": ("sleep", "exercise", "run", "running", "mood", "health", "injury", "workout", "training"),
    "relationships": ("friend", "message", "talk", "spoke", "contact", "who", "relationship"),
    "time": ("schedule", "calendar", "when", "free", "busy", "appointment", "availability"),
    "interests": ("browsing", "reading", "hobby", "watch", "interested", "learning"),
    "resources": ("spend", "spent", "money", "cost", "budget", "bought", "purchase"),
}


def _explicit_time_range(query_text: str) -> Optional[Tuple[str, str]]:
    from .retrieval import _iso_date_hints

    hints = _iso_date_hints(query_text)
    if not hints:
        return None
    days = sorted(hints)
    return (f"{days[0]}T00:00:00+00:00", f"{days[-1]}T23:59:59+00:00")


def _relative_time_range(query_text: str, now: datetime) -> Optional[Tuple[str, str]]:
    match = re.search(r"\blast (\d{1,3}) days\b", query_text, re.I)
    if match:
        start = now - timedelta(days=int(match.group(1)))
        return (
            start.strftime("%Y-%m-%dT00:00:00+00:00"),
            now.strftime("%Y-%m-%dT23:59:59+00:00"),
        )
    for pattern, resolver in _RELATIVE_RANGES:
        if resolver is None:
            continue
        if pattern.search(query_text):
            start, end = resolver(now)
            return (
                start.strftime("%Y-%m-%dT00:00:00+00:00"),
                end.strftime("%Y-%m-%dT23:59:59+00:00"),
            )
    return None


def _dimensions_for(query_text: str) -> List[str]:
    lowered = query_text.lower()
    out: List[str] = []
    for dimension, keywords in _DIMENSION_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(k)}", lowered) for k in keywords):
            out.append(dimension)
    return out


def _residual(query_text: str, entities: List[Dict[str, Any]]) -> str:
    residual = query_text
    for entity in entities:
        name = str(entity.get("canonical_name") or "")
        if name:
            residual = re.sub(re.escape(name), " ", residual, flags=re.I)
            first = name.split()[0]
            if len(first) > 2:
                residual = re.sub(rf"\b{re.escape(first)}\b", " ", residual, flags=re.I)
    residual = re.sub(
        r"\b(today|yesterday|this week|last week|this month|last month|last \d+ days)\b",
        " ",
        residual,
        flags=re.I,
    )
    return " ".join(residual.split())


def build_query_plan(
    conn,
    query_text: str,
    *,
    now: Optional[datetime] = None,
) -> QueryPlan:
    q = str(query_text or "").strip()
    now = now or datetime.now(timezone.utc)
    plan = QueryPlan(query_text=q)
    if not q:
        return plan

    if conn is not None:
        try:
            from ..features.entities.linking import link_query_entities

            plan.entities = link_query_entities(conn, q)
        except Exception:
            plan.entities = []

    plan.time_range = _explicit_time_range(q) or _relative_time_range(q, now)
    plan.aggregate_intent = bool(_AGGREGATE_RE.search(q))
    if _PAST_RE.search(q):
        plan.temporal_shift = "past"
    plan.dimensions = _dimensions_for(q)
    plan.semantic_residual = _residual(q, plan.entities)
    return plan
