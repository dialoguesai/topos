"""The retirement path for keyword routing — machinery, gates, and a flag that is off.

THE PROBLEM THIS ANSWERS
========================
Scope routing today is a pile of regexes: ~24 ``_ScopeRule`` predicates and ~10
``_CompositeRecipe`` exceptions in ``control_plane/scope_query_router.py``, mirrored
token-for-token in ``next/lib/mcp/scopeRoutingRules.ts``. It is well understood that a
keyword rule cannot express a compound request — the composite recipes ARE that
admission, one hand-written conjunction per compound question anybody has thought of.
Every defect found since has been repaired by adding another rule, and the pile has no
retirement path: no rule has ever been deleted, because nothing measures what deleting
one would cost.

Meanwhile Horos (``scope_head.py``) is already scoring real traffic on this node in
shadow mode, and ``scope_shadow.routing_comparison`` already answers "would the head
have routed this turn the way the rules did". The endgame follows from those two facts:
**the rules become priors and fallback, the head routes, and the switch between them is
a threshold on published numbers.**

WHAT SHIPS HERE, AND WHAT DOES NOT
==================================
Ships: the arbitration (:func:`arbitrate`), the promotion gates (:class:`RoutingGates`),
and the measurement that evaluates them (:func:`evaluate_promotion`, fed by
``scope_shadow.routing_comparison``).

Does **not** ship: head-decided routing. :func:`head_routes_enabled` is ``False`` unless
``TOPOS_SCOPE_HEAD_ROUTES`` is set, there is a test asserting that, and with the flag off
:func:`arbitrate` returns the rule priors byte-identically. On this node's own captured
traffic the head is not close to the gates (see ``NOTES_HOROS_ROUTING_ENDGAME.md``), so
shipping the switch already flipped would be a large, measurable regression.

Not retired here: **no keyword rule is deleted by this change.** Retirement is what the
gates authorise, and the gates need traffic that does not exist yet.

THREE RULES THIS FILE WILL NOT BEND
===================================
**The head may name scopes; it may never widen disclosure.** A routing model has no
signal about access modes and was never trained on one. A scope the head names that no
rule predicted is queried at :data:`MODE_FLOOR` — ``summary`` — never ``raw`` or
``inference``. Routing is not authorization (``scope_classifier`` §6A.4) and this is
where that stops being a slogan: without the floor, promoting a routing model would
silently promote a disclosure decision too.

**Uncertainty falls back to the rules, not to silence.** The measured failure mode is
not the head choosing wrongly, it is the head choosing *nothing* — ``dead`` on 59.6% of
observed routed turns, against the card's held-out 14.9%. So ``ambiguity``,
``ignorance`` and ``confident-none`` all defer to the priors. A head that abstains
where a rule fired must never be able to empty an answer.

**Every arbitration outcome is a closed-set slug.** These strings reach the narrowing
ledger, which crosses the node boundary. They are enums by construction, and any free
content belongs in the ledger's local-only ``detail``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The flag. **Default off**, and ``test_head_routing_is_off_by_default`` says so.
#: Nothing in this repo sets it; flipping it is a release decision that
#: :func:`evaluate_promotion` has to have cleared first.
ENV_HEAD_ROUTES = "TOPOS_SCOPE_HEAD_ROUTES"

#: Access mode a head-named scope gets when no rule predicted it. See the module
#: docstring: the head has no disclosure opinion, so it gets the narrowest one.
MODE_FLOOR = "summary"

#: Mirrors ``control_plane.scope_query_router.MODE_RANK``. Duplicated rather than
#: imported because the engine does not depend on the control plane — if the two ever
#: disagree, :func:`arbitrate` is the more conservative of the pair by construction,
#: since it only ever picks a mode a rule prior already carried.
_MODE_RANK: Dict[str, int] = {"summary": 0, "inference": 1, "raw": 2}

#: Must equal ``MAX_SCOPE_ROUTES`` in the control plane and the front end. A head that
#: routes to more scopes than the rules were allowed to is not an improvement, it is a
#: budget breach — each route costs a round trip and a slice of synthesis context.
MAX_SCOPE_ROUTES = 4

# --- arbitration outcomes (closed set; these reach the narrowing ledger) -------------
ARB_HEAD_DISABLED = "head_disabled"        # flag off — rules decided, unchanged
ARB_HEAD_UNAVAILABLE = "head_unavailable"  # flag on, no verdict to arbitrate with
ARB_HEAD_UNCERTAIN = "head_uncertain"      # ambiguity / ignorance — rules decided
ARB_HEAD_SILENT = "head_silent"            # confident-none over a rule match — rules decided
ARB_HEAD_ROUTED = "head_routed"            # head decided the scope set
ARB_NO_PRIOR = "no_prior"                  # head named a scope no rule predicted
ARB_MODE_FLOORED = "mode_floored"          # that scope was clamped to MODE_FLOOR
ARB_CAPPED = "capped"                      # MAX_SCOPE_ROUTES cut the tail

SOURCE_RULES = "rules"
SOURCE_HEAD = "head"


def head_routes_enabled() -> bool:
    """Is the head allowed to decide the scope set? **No, unless explicitly told.**

    Deliberately env-only, with no flag-file twin. ``scope_shadow`` has one because
    observation had to be reachable inside the macOS app shell, which inherits no
    environment — but that argument is about turning *telemetry* on. Turning
    *production routing* over to a model should require the operator to say so in a
    place that does not survive a stray ``touch``.
    """
    return str(os.environ.get(ENV_HEAD_ROUTES) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class RulePrior:
    """One keyword rule's opinion — a PRIOR, which is all a regex was ever entitled to be.

    Built from the incumbent router's output: ``rule_id`` and ``priority`` are its own
    (``_ScopeRule.id`` / ``.priority``), and lower priority means "fires first", matching
    ``_match_scope_candidates``. ``composite`` marks a prior that came from an exclusive
    ``_CompositeRecipe`` — the recipes are the rule pile's own admission that keywords
    cannot express compound asks, and they are the first candidates for retirement, so
    they stay distinguishable rather than being flattened into the rest.
    """

    scope_id: str
    access_mode: str = MODE_FLOOR
    rule_id: str = ""
    priority: int = 1000
    composite: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    """What to query, who decided it, and why — in slugs the ledger can carry."""

    routes: Tuple[Tuple[str, str], ...]      # (scope_id, access_mode), in query order
    source: str                              # SOURCE_RULES | SOURCE_HEAD
    arbitration: str                         # one ARB_* slug: the deciding outcome
    #: Every ARB_* slug that applied, in order. The deciding one is first. Closed set.
    notes: Tuple[str, ...] = ()
    head_confidence: float = 0.0

    @property
    def scopes(self) -> Tuple[str, ...]:
        return tuple(scope for scope, _mode in self.routes)

    def as_telemetry(self) -> Dict[str, Any]:
        """Counts, scope ids and closed-set slugs. Follows ``scope_shadow.as_telemetry``.

        No text, and no confidence-to-two-decimals per scope: a per-scope score vector
        over a fixed taxonomy is a fingerprint of the question, and the aggregate is
        what a promotion decision needs anyway.
        """
        return {
            "source": self.source,
            "arbitration": self.arbitration,
            "notes": list(self.notes),
            "scopes": list(self.scopes),
            "n_scopes": len(self.routes),
        }


def _priors_to_routes(
    priors: Sequence[RulePrior], *, max_routes: int
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Merge priors the way ``_merge_scope_routes`` does: first fire wins the order, the
    widest requested mode wins the mode, and the tail past the cap is cut and recorded."""
    ordered = sorted(priors, key=lambda p: (p.priority, p.scope_id))
    modes: Dict[str, str] = {}
    order: List[str] = []
    for prior in ordered:
        mode = prior.access_mode if prior.access_mode in _MODE_RANK else MODE_FLOOR
        current = modes.get(prior.scope_id)
        if current is None:
            modes[prior.scope_id] = mode
            order.append(prior.scope_id)
        elif _MODE_RANK[mode] > _MODE_RANK[current]:
            modes[prior.scope_id] = mode
    notes: List[str] = []
    if len(order) > max_routes:
        notes.append(ARB_CAPPED)
    return [(s, modes[s]) for s in order[:max_routes]], notes


def arbitrate(
    priors: Sequence[RulePrior],
    verdict: Any = None,
    *,
    enabled: Optional[bool] = None,
    max_routes: int = MAX_SCOPE_ROUTES,
) -> RoutingDecision:
    """Decide the scope set from the rule priors and the head's verdict.

    ``verdict`` is a ``scope_classifier.ScopeVerdict`` (or ``None`` when no head is
    installed). ``enabled`` overrides :func:`head_routes_enabled`, for tests and for a
    caller that has its own kill switch; ``None`` means "ask the flag".

    THE ARBITRATION, stated once so it is not re-derived from the code:

    1. Flag off, or no verdict → the priors decide, **unchanged**. This is the shipping
       path and it is byte-identical to what the rules produce today.
    2. Head escalated (``ambiguity`` / ``ignorance``) → the priors decide. An uncertain
       model is exactly the case the keyword pile is still good for.
    3. Head named nothing while a rule fired (``confident-none``) → the priors decide.
       The head's confident-none is its single largest measured divergence from the
       rules on real traffic, and deferring to a model's silence deletes data from an
       answer with no way for the owner to see that it happened.
    4. Head named scopes → the **head** decides the set. Each scope keeps its rule
       prior's access mode where one exists, because the head has no opinion about
       disclosure; a scope no rule predicted is queried at :data:`MODE_FLOOR`.
    5. Head named nothing and no rule fired → nothing is routed. Both agree.

    The asymmetry in 3 and 4 is deliberate and is the whole safety argument: the head
    can only ever ADD a scope or reorder the set. It cannot empty it, and it cannot
    raise an access mode. The worst promotion outcome is therefore over-retrieval
    inside modes the rules already authorised — never a silent hole, never a wider
    disclosure than a keyword rule would have asked for.
    """
    active = head_routes_enabled() if enabled is None else bool(enabled)
    rule_routes, rule_notes = _priors_to_routes(priors, max_routes=max_routes)

    def _rules(arbitration: str, extra: Sequence[str] = ()) -> RoutingDecision:
        return RoutingDecision(
            routes=tuple(rule_routes),
            source=SOURCE_RULES,
            arbitration=arbitration,
            notes=(arbitration, *extra, *rule_notes),
            head_confidence=float(getattr(verdict, "confidence", 0.0) or 0.0),
        )

    if not active:
        return _rules(ARB_HEAD_DISABLED)
    if verdict is None:
        return _rules(ARB_HEAD_UNAVAILABLE)
    if getattr(verdict, "escalated", False):
        return _rules(ARB_HEAD_UNCERTAIN)

    labels = tuple(str(x) for x in (getattr(verdict, "labels", ()) or ()))
    if not labels:
        # `confident-none` where the rules routed is the case worth naming separately:
        # it is not "no signal", it is the head asserting no data is needed, against a
        # rule that asserted the opposite. Falling back is not treating them as equal —
        # it is refusing to let the newer, unpromoted component be the one that empties
        # the answer.
        return _rules(ARB_HEAD_SILENT if rule_routes else ARB_HEAD_DISABLED)

    modes = {scope: mode for scope, mode in rule_routes}
    prior_order = [scope for scope, _mode in rule_routes]
    routes: List[Tuple[str, str]] = []
    notes: List[str] = [ARB_HEAD_ROUTED]
    # Scopes both agreed on keep the rules' order, so a promoted head does not silently
    # reshuffle which lane lands first in the synthesis context. Head-only scopes follow.
    for scope in [s for s in prior_order if s in labels] + [
        s for s in labels if s not in modes
    ]:
        mode = modes.get(scope)
        if mode is None:
            mode = MODE_FLOOR
            if ARB_NO_PRIOR not in notes:
                notes.extend((ARB_NO_PRIOR, ARB_MODE_FLOORED))
        routes.append((scope, mode))
    if len(routes) > max_routes:
        notes.append(ARB_CAPPED)
        routes = routes[:max_routes]

    return RoutingDecision(
        routes=tuple(routes),
        source=SOURCE_HEAD,
        arbitration=ARB_HEAD_ROUTED,
        notes=tuple(notes),
        head_confidence=float(getattr(verdict, "confidence", 0.0) or 0.0),
    )


# --- promotion gates -----------------------------------------------------------------


@dataclass(frozen=True)
class RoutingGates:
    """What has to be true of REAL traffic before :data:`ENV_HEAD_ROUTES` may be flipped.

    The training gates in ``scripts/train_scope_head.py`` decide whether an artifact may
    be *published*. These decide whether a published artifact may *route*, and they are
    a different question measured on a different population: held-out synthetic rows
    there, this node's captured traffic here. The card's v2 numbers clear the training
    gates on nothing except ``negatives_abstained``; measured against the shadow log its
    ``dead_rate`` is four times its held-out value. That gap is the reason this second
    set of gates exists at all.

    **A promotion is an arithmetic result, not a call.** :func:`evaluate_promotion`
    takes the dict ``scope_shadow.routing_comparison`` produces and returns the list of
    clauses that failed. Empty list, promote. Non-empty, do not. The thresholds below
    are a judgement — someone chose the numbers — but applying them is not, and nobody
    gets to weigh them against a demo.

    Every threshold is stated against the metric name the Horos card already publishes,
    so "the head is at 0.17 exact against a 0.70 gate" is checkable by anyone reading
    the card and the log, without access to this file.
    """

    #: Enough turns for any of the rates below to mean anything. 400 is ~8x the traffic
    #: captured to date and about the point where a 5% dead-rate gate has a usable
    #: confidence interval; it is a floor on evidence, not a target to farm.
    min_turns: int = 400

    #: Per-scope evidence. A headline rate over 400 turns can be carried entirely by two
    #: popular scopes while a third has never been observed at all — and an unobserved
    #: scope is precisely where a promoted head would fail unnoticed.
    min_turns_per_scope: int = 20

    #: Turn-level set agreement with the rules. Below this the head is not routing the
    #: same product. Matches the card's ``exact``.
    min_exact: float = 0.70

    #: The head names NOTHING where the rules routed. The dominant real-traffic failure
    #: and the one the owner cannot see: an answer written from no data reads exactly
    #: like an answer written from data that had nothing in it. Tighter than the
    #: training gate's ``GATE_DEAD_RATE = 0.20`` on purpose — that gate governs whether
    #: an artifact is worth publishing, this one governs whether it may empty a lane.
    max_dead_rate: float = 0.05

    #: The head acted on a scope set sharing nothing with the rules. Same value as
    #: ``GATE_DISJOINT_RATE`` because it is the same failure: acting on an unrelated
    #: scope is the permission-boundary error.
    max_disjoint_rate: float = 0.03

    #: Handoffs to the LLM. The escalation band is the flywheel and must stay open, but
    #: a head that escalates a third of production is not routing, it is a router-shaped
    #: delay in front of one.
    max_escalation_rate: float = 0.15

    #: Per-scope recall floor, the ``scopes_below_floor`` axis. Same 0.60 as the training
    #: gate: a scope the head names less than 60% of the time the rules do is a scope
    #: whose data would start going missing.
    min_scope_recall: float = 0.60


def _fmt(value: Optional[float]) -> str:
    return "unmeasured" if value is None else f"{value:.3f}"


def evaluate_promotion(
    comparison: Dict[str, Any], gates: Optional[RoutingGates] = None
) -> List[str]:
    """The gate clauses that FAILED. Empty list means the head may be promoted.

    ``comparison`` is ``scope_shadow.routing_comparison(ShadowLog().read())``.

    A conjunction, like the training gate: beating the rules on average is not the bar,
    because the average is not what an owner experiences — a single scope quietly going
    dead is.

    **Unmeasurable fails, it does not pass.** ``routing_comparison`` returns ``None``
    rather than ``0.0`` for a rate with no denominator, and a ``None`` here produces a
    failure naming what could not be measured. A gate that greenlights on an empty log
    is worse than no gate, because it looks like one.
    """
    gates = gates or RoutingGates()
    failures: List[str] = []

    turns = int(comparison.get("turns") or 0)
    if turns < gates.min_turns:
        failures.append(
            f"only {turns} routed turns observed, gate needs {gates.min_turns} — "
            f"every rate below is under-powered until then"
        )

    def _at_most(key: str, ceiling: float, what: str) -> None:
        value = comparison.get(key)
        if value is None:
            failures.append(f"{key} unmeasurable on this log ({what})")
        elif float(value) > ceiling:
            failures.append(f"{key} {_fmt(float(value))} > {ceiling} — {what}")

    def _at_least(key: str, floor: float, what: str) -> None:
        value = comparison.get(key)
        if value is None:
            failures.append(f"{key} unmeasurable on this log ({what})")
        elif float(value) < floor:
            failures.append(f"{key} {_fmt(float(value))} < {floor} — {what}")

    _at_least("exact", gates.min_exact, "the head is not routing the same product")
    _at_most(
        "dead_rate", gates.max_dead_rate,
        "that share of routed turns would retrieve nothing, invisibly",
    )
    _at_most(
        "disjoint_rate", gates.max_disjoint_rate,
        "acting on a scope set sharing nothing with the rules is the "
        "permission-boundary error",
    )
    _at_most(
        "escalation_rate", gates.max_escalation_rate,
        "a head that escalates this often is not routing",
    )

    scope_turns: Dict[str, int] = dict(comparison.get("scope_turns") or {})
    recalls: Dict[str, float] = dict(comparison.get("per_scope_recall") or {})
    thin = sorted(s for s, n in scope_turns.items() if n < gates.min_turns_per_scope)
    if thin:
        failures.append(
            f"{len(thin)} scopes have fewer than {gates.min_turns_per_scope} observed "
            f"turns: {thin}"
        )
    below = sorted(
        s for s, r in recalls.items()
        if scope_turns.get(s, 0) >= gates.min_turns_per_scope and r < gates.min_scope_recall
    )
    if below:
        failures.append(
            f"{len(below)} scopes below {gates.min_scope_recall} recall against the "
            f"rules: {below}"
        )
    # A live scope the log has never seen cannot be gated on, and silence about it
    # would read as a pass.
    try:
        from .scope_classifier import live_scope_ids

        unseen = sorted(set(live_scope_ids()) - set(scope_turns))
    except Exception:  # noqa: BLE001 — a registry read must not break a report
        logger.debug("live scope ids unavailable for promotion gate", exc_info=True)
        unseen = []
    if unseen:
        failures.append(f"{len(unseen)} live scopes never observed in the log: {unseen}")

    return failures


@dataclass
class PromotionStatus:
    """The published answer to "may the head route yet?" — numbers plus the verdict."""

    ready: bool
    failures: List[str] = field(default_factory=list)
    comparison: Dict[str, Any] = field(default_factory=dict)
    gates: Dict[str, Any] = field(default_factory=dict)

    def as_telemetry(self) -> Dict[str, Any]:
        """Text-free: ``routing_comparison`` is counts and scope ids, the failures are
        gate clauses naming metrics and thresholds. No owner text can reach either."""
        return {
            "ready": self.ready,
            "failures": list(self.failures),
            "comparison": dict(self.comparison),
            "gates": dict(self.gates),
            "head_routes_enabled": head_routes_enabled(),
        }


def promotion_status(
    rows: Sequence[Dict[str, Any]], gates: Optional[RoutingGates] = None
) -> PromotionStatus:
    """End to end: shadow-log rows in, promotion verdict out.

    The single entry point a status surface, a release check or a note should call, so
    the number quoted anywhere is the number this function produced.
    """
    from dataclasses import asdict

    from .scope_shadow import routing_comparison

    gates = gates or RoutingGates()
    comparison = routing_comparison(rows)
    failures = evaluate_promotion(comparison, gates)
    return PromotionStatus(
        ready=not failures,
        failures=failures,
        comparison=comparison,
        gates=asdict(gates),
    )
