"""Q3 — the window comes from the DATA, not from the sentence.

*"What did I miss while I was heads-down on the classifier?"* names no dates. Every
window this pipeline could build until now was parsed out of the words: an explicit
range ("Aug 4-11"), a relative phrase ("last week"), a differenced pair ("this week vs
last"). None of them can answer that question, because the period the owner means is
not in the sentence — it is in their own activity, and only the node can see it.

So the anchor is resolved the other way round: the sentence names a SUBJECT, the
subject's own mention density names the PERIOD.

THE METHOD, STATED (``mention_density_peak_span``)
--------------------------------------------------
One method, chosen because it is the one a person can argue with:

1. Every mention of the resolved entity is bucketed by calendar day. The entity's
   *observed span* is its first mention day through its last.
2. A day is HOT when it carries at least ``threshold`` mentions, where threshold is
   the entity's mean daily rate over that span, floored at one. The floor matters: an
   entity mentioned 20 times over 90 days has a mean of 0.22, and without the floor
   every day it appears on would be "above average", which is not a period.
3. Hot days are joined into runs. A run tolerates up to :data:`GAP_DAYS` consecutive
   cold days inside it, because a heads-down stretch survives a weekend, and is
   trimmed so it starts and ends hot. The run with the most mentions wins; ties go to
   the most recent, since "while I was heads-down" is a recent-past frame.
4. The winning run must clear three floors or NO WINDOW IS RETURNED — see below.

Alternatives considered and rejected: a fixed trailing window (that is the sentence's
window again, wearing a hat); a changepoint fit (needs more days than this corpus has,
and its output is a boundary rather than a span the owner can read); a percentile cut
(a percentile of five active days is not a statistic).

REFUSAL IS THE FEATURE
----------------------
An entity with three scattered mentions has no heads-down period, and the honest
answer is that no window could be derived. Every refusal is a closed-set slug from
:data:`REFUSALS`, carried as a lane-level empty cause exactly the way Q1's per-goal
report carries "no evidence found" — never as a plausible-looking range. Sparse nodes
will hit this constantly and that is correct behaviour, not a degraded mode.

The three floors, each with its own refusal:

* ``entity_window_density_too_thin`` — fewer than :data:`MIN_TOTAL_MENTIONS` mentions,
  fewer than :data:`MIN_ACTIVE_DAYS` days carrying them, or fewer than
  :data:`MIN_WINDOW_MENTIONS` inside the winning run. There is not enough evidence for
  a period to exist.
* ``entity_window_density_uniform`` — mentions exist and are evenly spread: the
  winning run's daily rate is under :data:`MIN_RATE_LIFT`x the rate outside it. A
  subject you touch every other day for two months is a habit, not a stretch.
* ``entity_window_span_too_broad`` — the run clears everything but is longer than
  :data:`MAX_WINDOW_DAYS`. A "heads-down period" of three months is a guess with a
  date range stapled to it.

WHAT LEAVES THE NODE
--------------------
:meth:`DerivedWindow.as_ledger_public` is enums and integers only. The derived range
is CONTENT — it describes when the owner was busy — so it lives in the ledger's
local-only ``detail`` and in the packet's own ``time_window`` block, which is where
the parsed window has always been published and which the owner has to be able to
read in order to dispute it ("I think you meant Aug 4-11" is only useful if it is
shown). ``method`` and every refusal are slugs from the sets declared here.

Nothing in this module touches a database. The caller gathers the day counts, having
already put the mentions through the privacy plane; this decides what shape they are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

# --- the anchoring frame ---------------------------------------------------------------
#: "while I was heads-down on X" and its neighbours. Deliberately narrow, and
#: deliberately requires the SUBORDINATE frame ("while/when/during ... I was ..."):
#: without it, "what did I miss about the classifier" is an ordinary subject question
#: that the scope routes already answer over the ordinary window, and hijacking its
#: window would change an answer that is not broken.
_ANCHOR_RE = re.compile(
    r"\b(?:while|when|during|whilst)\b[^?.!]{0,60}?"
    r"\b(?:heads[- ]?down|head[- ]?down|deep in|buried in|focus(?:s?ed|ing)? on|"
    r"absorbed in|consumed by|busy with|working (?:on|through)|"
    r"in the (?:middle|thick) of|(?:the )?\w+ (?:push|crunch|sprint|grind))\b",
    re.I,
)

#: The complementary half: the question has to be about what the period COST. "While I
#: was heads-down on the classifier, did Sarah reply?" is a lookup, not a triage, and
#: it wants the ordinary lanes. Both halves must match before a window is derived.
_MISSED_RE = re.compile(
    r"\b(?:miss(?:ed|ing)?|slip(?:ped)?|fell through|fall through|overlook(?:ed)?|"
    r"neglect(?:ed)?|pile[d]? up|backlog|go(?:t|ne)? past me|pass(?:ed)? me by|"
    r"not (?:see|notice)|didn'?t (?:see|notice|catch)|need(?:s|ed)? my attention|"
    r"deserved? (?:my )?attention|distract(?:ed|ion|ions)?|triage)\b",
    re.I,
)


def entity_anchor_intent(query_text: str) -> bool:
    """True when the sentence anchors a window to an activity instead of naming one.

    Both halves are required — the frame ("while I was heads-down on …") and the cost
    ("what did I miss"). One without the other is a question the existing lanes already
    answer correctly, and this mode may not quietly re-window it.
    """
    text = str(query_text or "")
    if not text:
        return False
    return bool(_ANCHOR_RE.search(text)) and bool(_MISSED_RE.search(text))


# --- the floors ------------------------------------------------------------------------
#: Mentions the entity needs across its whole observed span before a period can exist.
MIN_TOTAL_MENTIONS = 5
#: Distinct days those mentions must fall on. Five mentions in one hour is one event.
MIN_ACTIVE_DAYS = 3
#: Mentions the winning run must itself carry.
MIN_WINDOW_MENTIONS = 3
#: In-window daily rate ÷ out-of-window daily rate. Below this the subject is a habit.
MIN_RATE_LIFT = 1.5
#: Cold days tolerated inside a run. A heads-down stretch survives a weekend.
GAP_DAYS = 2
#: The longest thing that may be called a heads-down period.
MAX_WINDOW_DAYS = 31
#: When the winning run swallows the entity's WHOLE observed span there is nothing to
#: measure a lift against — the run narrowed nothing. That is still an honest period if
#: the span is itself compact (a subject that existed for nine days and then stopped IS
#: those nine days); past this it is a subject you touch steadily, which has no
#: heads-down stretch to find.
MAX_UNSPLIT_SPAN_DAYS = 14

#: The one method this module implements, as a slug. Published so a consumer can tell
#: an entity-anchored window from a parsed one without parsing prose.
METHOD_PEAK_SPAN = "mention_density_peak_span"

# --- closed-set refusals ---------------------------------------------------------------
REFUSAL_UNRESOLVED = "entity_window_unresolved"
REFUSAL_NO_MENTIONS = "entity_window_no_mentions"
REFUSAL_TOO_THIN = "entity_window_density_too_thin"
REFUSAL_UNIFORM = "entity_window_density_uniform"
REFUSAL_TOO_BROAD = "entity_window_span_too_broad"

REFUSALS = frozenset(
    {
        REFUSAL_UNRESOLVED,
        REFUSAL_NO_MENTIONS,
        REFUSAL_TOO_THIN,
        REFUSAL_UNIFORM,
        REFUSAL_TOO_BROAD,
    }
)

#: The ledger reason for a window that WAS derived.
REASON_DERIVED = "entity_window_derived"


@dataclass
class DerivedWindow:
    """A window the data proposed, or the refusal that says why it could not.

    Exactly one of ``start``/``end`` and ``refusal`` is set. ``refusal`` is always a
    member of :data:`REFUSALS`; ``method`` is always :data:`METHOD_PEAK_SPAN`.
    """

    start: Optional[str] = None           # ISO date, inclusive
    end: Optional[str] = None             # ISO date, inclusive
    refusal: Optional[str] = None
    method: str = METHOD_PEAK_SPAN
    mentions_total: int = 0
    mentions_in_window: int = 0
    active_days: int = 0
    window_days: int = 0
    span_days: int = 0
    #: In-window rate ÷ out-of-window rate, rounded. ``None`` when the entity has no
    #: mentions outside the window (nothing to compare against).
    rate_lift: Optional[float] = None
    #: Entity ids the density was built from. Ids, never names.
    entity_ids: List[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.start and self.end and not self.refusal)

    def time_range(self) -> Optional[Tuple[str, str]]:
        """The plan's window shape: inclusive ISO instants, UTC."""
        if not self.resolved:
            return None
        return (f"{self.start}T00:00:00+00:00", f"{self.end}T23:59:59+00:00")

    def as_ledger_public(self) -> Dict[str, Any]:
        """Enums and integers. Safe for a slug-only surface — carries NO dates."""
        out: Dict[str, Any] = {
            "method": self.method,
            "mentions_total": self.mentions_total,
            "mentions_in_window": self.mentions_in_window,
            "active_days": self.active_days,
            "window_days": self.window_days,
            "span_days": self.span_days,
            "entities": len(self.entity_ids),
        }
        if self.refusal:
            out["refusal"] = self.refusal
        return out

    def as_packet(self) -> Dict[str, Any]:
        """What the caller is shown, so the caller can DISPUTE it.

        A derived window that the owner cannot see is a window they cannot correct,
        and a wrong one would then silently shape every lane in the turn. The dates
        ride here — the same field the parsed window has always been published on —
        and never on a closed-set slug.
        """
        out: Dict[str, Any] = {
            "source": "entity_mention_density",
            "method": self.method,
            "mentions_total": self.mentions_total,
            "active_days": self.active_days,
        }
        if self.resolved:
            out["from"] = self.start
            out["to"] = self.end
            out["window_days"] = self.window_days
            out["mentions_in_window"] = self.mentions_in_window
            if self.rate_lift is not None:
                out["rate_lift"] = self.rate_lift
        else:
            out["empty_reason"] = self.refusal
        return out


def _parse_day(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _runs(days: List[date], counts: Dict[date, int], threshold: float) -> List[Tuple[date, date]]:
    """Hot-day runs over the observed span, gap-tolerant and hot-trimmed."""
    first, last = days[0], days[-1]
    runs: List[Tuple[date, date]] = []
    run_start: Optional[date] = None
    run_last_hot: Optional[date] = None
    cold_streak = 0
    cursor = first
    while cursor <= last:
        hot = counts.get(cursor, 0) >= threshold
        if hot:
            if run_start is None:
                run_start = cursor
            run_last_hot = cursor
            cold_streak = 0
        elif run_start is not None:
            cold_streak += 1
            if cold_streak > GAP_DAYS:
                runs.append((run_start, run_last_hot or run_start))
                run_start = None
                run_last_hot = None
                cold_streak = 0
        cursor += timedelta(days=1)
    if run_start is not None:
        runs.append((run_start, run_last_hot or run_start))
    return runs


def derive_window_from_days(
    day_counts: Dict[str, int],
    *,
    entity_ids: Optional[List[str]] = None,
) -> DerivedWindow:
    """The whole method, over already-gathered day counts. No I/O, no clock.

    ``day_counts`` maps ISO dates to mention counts; unparseable keys are dropped
    rather than guessed at, because a mention whose date cannot be read cannot
    evidence a period.
    """
    ids = [str(x) for x in (entity_ids or [])]
    counts: Dict[date, int] = {}
    for raw_day, raw_count in (day_counts or {}).items():
        day = _parse_day(raw_day)
        if day is None:
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[day] = counts.get(day, 0) + count

    total = sum(counts.values())
    active_days = len(counts)
    if total <= 0:
        return DerivedWindow(refusal=REFUSAL_NO_MENTIONS, entity_ids=ids)

    days = sorted(counts)
    span_days = (days[-1] - days[0]).days + 1
    base = DerivedWindow(
        mentions_total=total,
        active_days=active_days,
        span_days=span_days,
        entity_ids=ids,
    )
    if total < MIN_TOTAL_MENTIONS or active_days < MIN_ACTIVE_DAYS:
        base.refusal = REFUSAL_TOO_THIN
        return base

    threshold = max(1.0, total / float(span_days))
    runs = _runs(days, counts, threshold)
    if not runs:
        # Unreachable while the threshold is floored at one and a day exists with a
        # count — kept because the floor is a constant somebody may one day raise.
        base.refusal = REFUSAL_TOO_THIN
        return base

    def _mass(run: Tuple[date, date]) -> int:
        start, end = run
        return sum(c for d, c in counts.items() if start <= d <= end)

    # Most mentions wins; ties to the most recent run.
    best = max(runs, key=lambda run: (_mass(run), run[1].toordinal()))
    mass = _mass(best)
    window_days = (best[1] - best[0]).days + 1
    base.mentions_in_window = mass
    base.window_days = window_days

    if mass < MIN_WINDOW_MENTIONS:
        base.refusal = REFUSAL_TOO_THIN
        return base

    outside_days = span_days - window_days
    outside_mass = total - mass
    if outside_days > 0:
        in_rate = mass / float(window_days)
        out_rate = outside_mass / float(outside_days)
        lift = (in_rate / out_rate) if out_rate > 0 else None
        base.rate_lift = round(lift, 2) if lift is not None else None
        if lift is not None and lift < MIN_RATE_LIFT:
            base.refusal = REFUSAL_UNIFORM
            return base
    elif window_days > MAX_UNSPLIT_SPAN_DAYS:
        # The run IS the observed span: no contrast exists, so no lift can be computed
        # and the "window" would return the entity's entire history under a name that
        # claims it is a period. Refusing is the whole point of the mode.
        base.refusal = REFUSAL_UNIFORM
        return base

    if window_days > MAX_WINDOW_DAYS:
        base.refusal = REFUSAL_TOO_BROAD
        return base

    base.start = best[0].isoformat()
    base.end = best[1].isoformat()
    return base
