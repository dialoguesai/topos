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
    # Point-in-time read anchor (ISO date, YYYY-MM-DD): "in March" / "in March
    # 2024" resolves to the last day of that (past) month. Consumed by the fact
    # lane: facts valid AT as_of answer, without the stale marker (B1.1/T4).
    as_of: Optional[str] = None
    # First-person identity/belief/preference/possession phrasing ("what do I
    # think", "my hobbies", "am I", "have I sent") — conservative v1 set that
    # deliberately does NOT trip on artifact possessives ("my meeting notes",
    # "my calendar"). Drives owner-authored preference in retrieval (P3.3).
    first_person_intent: bool = False
    # Belief/identity subclass of first_person_intent: rows entering the answer
    # must be owner-authored (hard filter); the broader flag only re-ranks.
    first_person_belief: bool = False
    # "who/people do I talk to" interaction browse (relationship surface).
    interaction_browse: bool = False
    dimensions: List[str] = field(default_factory=list)
    semantic_residual: str = ""

    def to_meta(self) -> Dict[str, Any]:
        return {
            "entities": [e.get("canonical_name") for e in self.entities],
            "time_range": list(self.time_range) if self.time_range else None,
            "aggregate_intent": self.aggregate_intent,
            "temporal_shift": self.temporal_shift,
            "as_of": self.as_of,
            "first_person_intent": self.first_person_intent,
            "dimensions": self.dimensions,
        }


_AGGREGATE_RE = re.compile(
    r"\b(how often|how much|how many|how long|average|avg|typically|usually|"
    r"most active|per (?:day|week|month)|trend|total spend|spend on|rhythm|habit)\b",
    re.I,
)

_PAST_RE = re.compile(r"\b(before|prior|previous|previously|used to|former|formerly)\b", re.I)

# --- as-of month derivation (B1.1) ----------------------------------------------------
# "in <Month>" / "in <Month> 20xx" resolves to the LAST DAY of that month (ISO
# date). Bare months resolve to the most recent PAST occurrence relative to
# `now`; current/future months never produce an as_of (present-tense reads stay
# on the active fact chain). A day number right after the month ("in March 13")
# is a specific date, handled by the explicit time-range path — not an as-of.
_AS_OF_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_AS_OF_RE = re.compile(
    r"\bin\s+(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"\.?(?:\s+(20\d{2}))?\b",
    re.I,
)


def derive_as_of(query_text: str, now: datetime) -> Optional[str]:
    """ISO date (YYYY-MM-DD) for an "in <Month> [year]" point-in-time phrase.

    Last day of the referenced month; None when no month phrase, when a day
    number follows the month (explicit date, not an as-of), or when the month
    is not strictly in the past (bare month = most recent past occurrence)."""
    text = query_text or ""
    for match in _AS_OF_RE.finditer(text):
        rest = text[match.end():]
        if re.match(r"\s+\d{1,2}(?!\d)", rest):
            continue  # "in March 13" — a date, not a month-granular as-of
        month = _AS_OF_MONTHS[match.group(1).lower()]
        year_raw = match.group(2)
        if year_raw:
            year = int(year_raw)
        elif month < now.month:
            year = now.year
        else:
            # Bare month = most recent PAST occurrence: a month that hasn't
            # finished (including the current one) refers to last year's.
            year = now.year - 1
        if (year, month) >= (now.year, now.month):
            continue  # explicit current/future months never anchor a past read
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        next_month_start = (month_start + timedelta(days=32)).replace(day=1)
        last_day = next_month_start - timedelta(days=1)
        return last_day.date().isoformat()
    return None


# --- first-person intent (P3.3) --------------------------------------------------------
# Conservative v1: identity/belief/preference head-nouns and explicit
# first-person verb frames only. Artifact possessives ("my meeting notes",
# "my calendar", "my messaging cadence", "my contact Alex") must NOT match.
_FP_BELIEF_RE = re.compile(
    r"(?:\bdo i (?:\w+ ){0,2}?(?:think|feel|believe)\b"
    r"|\bam i (?:interested|into)\b"
    r"|\bhave i (?:ever )?(?:said|mentioned|expressed|claimed)\b"
    r"|\bmy (?:\w+ )?(?:opinions?|beliefs?|stances?|views?|interests?"
    r"|hobb(?:y|ies)|preferences?)\b"
    r")",
    re.I,
)
_FP_INTERACTION_RE = re.compile(
    r"\b(?:who|people)\b[^?.!]{0,40}?\bi (?:talk|speak|interact|chat) (?:to|with)\b",
    re.I,
)
_FP_AUTHORED_STAT_RE = re.compile(
    r"\b(?:have|did) i (?:sent|send)\b|\bmessages i(?:'ve| have)? sent\b",
    re.I,
)
_FP_GOALS_RE = re.compile(r"\bmy (?:\w+ )?goals?\b", re.I)


def first_person_flags(query_text: str) -> Tuple[bool, bool, bool]:
    """(first_person_intent, first_person_belief, interaction_browse)."""
    q = query_text or ""
    belief = bool(_FP_BELIEF_RE.search(q))
    interaction = bool(_FP_INTERACTION_RE.search(q))
    intent = (
        belief
        or interaction
        or bool(_FP_AUTHORED_STAT_RE.search(q))
        or bool(_FP_GOALS_RE.search(q))
    )
    return intent, belief, interaction

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
    plan.as_of = derive_as_of(q, now)
    (
        plan.first_person_intent,
        plan.first_person_belief,
        plan.interaction_browse,
    ) = first_person_flags(q)
    plan.dimensions = _dimensions_for(q)
    plan.semantic_residual = _residual(q, plan.entities)
    return plan
