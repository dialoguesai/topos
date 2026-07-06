"""Pre-retrieval intent qualifier + counter-offer (plan §C).

The cognitive firewall's negotiation layer: instead of a bare denial, an under-specified or
over-broad request gets a *machine-readable counter-offer* that tells the requester's AI
exactly what to narrow. This runs BEFORE retrieval and touches NO user data — it judges only
whether the stated intent is specific and proportional to the granted scope, so it is fast
and leaks nothing.

Deterministic checks first (this module). An optional bounded-LLM specificity score can layer
on later; the deterministic layer is the enforceable core and the CI-testable contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .types import MODE_RANK

# --- policy ---------------------------------------------------------------------------------

# Broad/greedy phrasings that signal an unproportioned request ("give me everything").
_BROAD_PATTERNS = (
    "everything",
    "all your",
    "all of your",
    "all data",
    "all the data",
    "anything",
    "any data",
    "full history",
    "entire",
    "whole history",
    "dump",
    "give me all",
    "raw data",
    "no filter",
    "unfiltered",
)

# Minimal stopword set for specificity counting (not linguistic completeness — just enough to
# tell "everything" from "messages with Alex about Q3 launch last week").
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "to", "for", "and", "or", "me", "my", "your", "you", "i",
        "is", "are", "do", "does", "did", "give", "show", "tell", "get", "list", "about",
        "what", "who", "when", "where", "how", "any", "all", "some", "please", "with", "on",
        "in", "at", "from", "this", "that", "these", "those", "want", "need", "can", "could",
    }
)

_MIN_SPECIFIC_TOKENS = 2

# Scopes where an unbounded time range is disproportionate; the requester must bound it.
_TIME_BOUNDED_SCOPES = frozenset(
    {"availability:read", "messages:read", "ai_conversations:read", "activity:read"}
)
_DEFAULT_TIME_WINDOW_MAX_DAYS = 30

# Terms that indicate the intent already carries a time bound (so we don't demand one).
_TIME_HINT_RE = re.compile(
    r"\b(today|yesterday|tomorrow|week|weeks|weekend|month|months|day|days|year|"
    r"morning|afternoon|evening|tonight|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|q1|q2|q3|q4|"
    r"last|next|recent|since|between|before|after)\b|\d{4}-\d{2}",
    re.I,
)

_ALL_MODES = ("summary", "inference", "raw")

_SUGGESTED_INTENTS: Dict[str, List[str]] = {
    "availability:read": [
        "Is there a free 30-minute window next week for a call?",
        "Which mornings are open Monday–Friday this week?",
    ],
    "messages:read": [
        "Summarize messages with Alex about the Q3 launch in the last 30 days",
    ],
    "ai_conversations:read": [
        "What conclusions were reached about the Kilnwatch debounce bug last month?",
    ],
    "relationship_context:read": [
        "How is the requester connected to Maya Chen, and when did they last interact?",
    ],
    "activity:read": [
        "What project was worked on most in the past two weeks?",
    ],
}
_DEFAULT_SUGGESTED = [
    "Ask a specific, purpose-bounded question about a single topic and time range.",
]

DEFAULT_MAX_ROUNDS = 3


# --- data model -----------------------------------------------------------------------------

# reason codes, most-structural first (used to pick the primary reason when several trip).
REASON_MODE_ABOVE_CEILING = "mode_above_ceiling"
REASON_PURPOSE_MISSING = "purpose_missing"
REASON_INTENT_TOO_BROAD = "intent_too_broad"
REASON_TIME_WINDOW_REQUIRED = "time_window_required"

_REASON_PRIORITY = (
    REASON_MODE_ABOVE_CEILING,
    REASON_PURPOSE_MISSING,
    REASON_INTENT_TOO_BROAD,
    REASON_TIME_WINDOW_REQUIRED,
)


@dataclass
class CounterOffer:
    reason: str
    scopes_available: List[str] = field(default_factory=list)
    access_modes: Dict[str, str] = field(default_factory=dict)
    requires: List[Dict[str, Any]] = field(default_factory=list)
    suggested_intents: List[str] = field(default_factory=list)
    round: int = 1
    max_rounds: int = DEFAULT_MAX_ROUNDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "scopes_available": list(self.scopes_available),
            "access_modes": dict(self.access_modes),
            "requires": list(self.requires),
            "suggested_intents": list(self.suggested_intents),
            "round": self.round,
            "max_rounds": self.max_rounds,
        }


@dataclass
class QualificationOutcome:
    ok: bool
    offer: Optional[CounterOffer] = None
    # True when the round budget is exhausted → caller should hard-deny, not re-offer.
    exhausted: bool = False


# --- checks ---------------------------------------------------------------------------------

def _specific_tokens(query_text: str) -> List[str]:
    tokens = re.findall(r"[\w']+", str(query_text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _is_broad(query_text: str) -> bool:
    low = str(query_text or "").lower()
    return any(p in low for p in _BROAD_PATTERNS)


def _has_time_bound(query_text: str, filter_manifest: Optional[Dict[str, Any]]) -> bool:
    if _TIME_HINT_RE.search(str(query_text or "")):
        return True
    if isinstance(filter_manifest, dict):
        fm = filter_manifest.get("filter_manifest") if isinstance(filter_manifest.get("filter_manifest"), dict) else filter_manifest
        for item in fm.get("filters") or []:
            if isinstance(item, dict) and item.get("filter_id") in ("rolling_window_days", "date_range"):
                return True
    return False


def _access_modes_for_ceiling(ceiling: str) -> Dict[str, str]:
    ceil_rank = MODE_RANK.get(str(ceiling or "summary"), MODE_RANK["summary"])
    modes: Dict[str, str] = {}
    for mode in _ALL_MODES:
        if MODE_RANK[mode] <= ceil_rank:
            modes[mode] = "available"
        else:
            modes[mode] = "requires_owner_approval"
    return modes


def qualify_intent(
    *,
    scope_id: str,
    access_mode: str,
    query_text: str,
    grant_ceiling: str = "summary",
    granted_scopes: Optional[List[str]] = None,
    filter_manifest: Optional[Dict[str, Any]] = None,
    round: int = 1,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> QualificationOutcome:
    """Judge whether *query_text* is specific and proportional to *scope_id* at *access_mode*.

    Returns ok=True to proceed, or ok=False with a CounterOffer describing what to narrow.
    Touches no user data. When the round budget is exhausted, returns exhausted=True so the
    caller hard-denies instead of looping forever.
    """
    scopes_available = list(granted_scopes or [scope_id])
    requires: List[Dict[str, Any]] = []
    reasons: set[str] = set()

    # 1. Mode proportionality — a mode above the grant ceiling needs owner approval, not a
    #    silent grant. (Reframes what the engine would otherwise hard-deny.)
    ceil_rank = MODE_RANK.get(str(grant_ceiling or "summary"), MODE_RANK["summary"])
    if MODE_RANK.get(str(access_mode), MODE_RANK["summary"]) > ceil_rank:
        reasons.add(REASON_MODE_ABOVE_CEILING)
        requires.append(
            {
                "type": "lower_mode",
                "available_modes": [m for m in _ALL_MODES if MODE_RANK[m] <= ceil_rank],
                "requested": access_mode,
            }
        )

    # 2. Purpose / breadth — the core "make the question specific" check.
    tokens = _specific_tokens(query_text)
    if not str(query_text or "").strip():
        reasons.add(REASON_PURPOSE_MISSING)
        requires.append({"type": "purpose_statement"})
    elif _is_broad(query_text) or len(tokens) < _MIN_SPECIFIC_TOKENS:
        reasons.add(REASON_INTENT_TOO_BROAD)
        requires.append({"type": "narrower_topic"})

    # 3. Time bound — disproportionate scopes must be time-bounded.
    if scope_id in _TIME_BOUNDED_SCOPES and not _has_time_bound(query_text, filter_manifest):
        reasons.add(REASON_TIME_WINDOW_REQUIRED)
        requires.append({"type": "time_window", "max_days": _DEFAULT_TIME_WINDOW_MAX_DAYS})

    if not reasons:
        return QualificationOutcome(ok=True)

    # Budget exhausted → the caller should hard-deny rather than offer again.
    if round > max_rounds:
        return QualificationOutcome(ok=False, exhausted=True)

    primary = next((r for r in _REASON_PRIORITY if r in reasons), sorted(reasons)[0])
    offer = CounterOffer(
        reason=primary,
        scopes_available=scopes_available,
        access_modes=_access_modes_for_ceiling(grant_ceiling),
        requires=requires,
        suggested_intents=list(_SUGGESTED_INTENTS.get(scope_id, _DEFAULT_SUGGESTED)),
        round=round,
        max_rounds=max_rounds,
    )
    return QualificationOutcome(ok=False, offer=offer)


# --- §C.3 owner-side clarification loop ----------------------------------------------------

_MODE_ORDER = ("summary", "inference", "raw")


@dataclass
class OwnerRecommendation:
    """The minimal proportional disclosure the owner is advised to approve."""

    scope_id: str
    access_mode: str
    time_window_days: Optional[int] = None
    label: str = "recommended"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "access_mode": self.access_mode,
            "time_window_days": self.time_window_days,
            "label": self.label,
        }


@dataclass
class OwnerClarification:
    """Owner-facing question for a request that needs approval, with a recommended default and
    a small set of options (the recommended minimal disclosure, a broader one, and deny)."""

    requester_id: str
    reason: str
    question: str
    recommended: OwnerRecommendation
    options: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "owner_clarification",
            "requester_id": self.requester_id,
            "reason": self.reason,
            "question": self.question,
            "recommended": self.recommended.to_dict(),
            "options": list(self.options),
        }


def _lowest_available_mode(access_modes: Dict[str, str]) -> str:
    for mode in _MODE_ORDER:
        if access_modes.get(mode) == "available":
            return mode
    return "summary"


def _requested_time_window(offer: CounterOffer) -> Optional[int]:
    for req in offer.requires:
        if req.get("type") == "time_window":
            return int(req.get("max_days") or _DEFAULT_TIME_WINDOW_MAX_DAYS)
    return None


def build_owner_clarification(
    *,
    offer: CounterOffer,
    requester_id: str = "requester",
    scope_id: Optional[str] = None,
) -> OwnerClarification:
    """From a counter-offer, build the owner-facing clarification. The recommendation is the
    MINIMAL proportional disclosure: the lowest available access mode, the granted scope, and
    a bounded time window when the scope calls for one."""
    scope = scope_id or (offer.scopes_available[0] if offer.scopes_available else "")
    rec_mode = _lowest_available_mode(offer.access_modes)
    window = _requested_time_window(offer)
    recommended = OwnerRecommendation(scope_id=scope, access_mode=rec_mode, time_window_days=window)

    window_txt = f", limited to the last {window} days" if window else ""
    question = (
        f"{requester_id} is requesting access to {scope}. "
        f"Recommended: share {rec_mode}-level only{window_txt}. Approve?"
    )

    # A broader alternative: the next mode up (may itself require care), plus an explicit deny.
    broader_mode = next(
        (m for m in _MODE_ORDER if _MODE_ORDER.index(m) > _MODE_ORDER.index(rec_mode)), None
    )
    options: List[Dict[str, Any]] = [
        {"choice": "accept_recommended", **recommended.to_dict()},
    ]
    if broader_mode:
        options.append({"choice": "broader", "scope_id": scope, "access_mode": broader_mode, "time_window_days": window})
    options.append({"choice": "deny"})

    return OwnerClarification(
        requester_id=requester_id,
        reason=offer.reason,
        question=question,
        recommended=recommended,
        options=options,
    )


def resolve_owner_decision(clarification: OwnerClarification, choice: str) -> Optional[Dict[str, Any]]:
    """Apply the owner's decision. Returns the effective grant params, or None on deny."""
    for opt in clarification.options:
        if opt.get("choice") == choice:
            if choice == "deny":
                return None
            return {k: v for k, v in opt.items() if k != "choice"}
    # Unknown choice → fail closed (deny).
    return None


def recommendation_acceptance_rate(decisions: List[str]) -> float:
    """Fraction of owner decisions that accepted the recommended minimal disclosure."""
    decisions = list(decisions)
    if not decisions:
        return 0.0
    return sum(1 for d in decisions if d == "accept_recommended") / len(decisions)


def build_narrow_request_response(
    *,
    offer: CounterOffer,
    scope_id: str,
    access_mode: str,
    session_id: str,
    audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Render a CounterOffer into the pipeline's response envelope. Additive to the existing
    turn_outcome vocabulary — never carries a public_result (nothing is disclosed on a
    narrow_request; the requester must re-ask more specifically)."""
    return {
        "turn_outcome": "narrow_request",
        "public_result": None,
        "reason": offer.reason,
        "offer": offer.to_dict(),
        "scope_id": scope_id,
        "access_mode": access_mode,
        "session_id": session_id,
        "query_session_id": session_id,
        "audit": audit or {},
    }
