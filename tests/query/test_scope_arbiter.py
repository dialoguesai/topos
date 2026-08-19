"""The promotion path: off by default, safe when on, and gated on published numbers.

The single most important test in this file is the first one. Everything else here is
machinery that does nothing until a flag is flipped, and the flag being off is the only
reason shipping unpromoted routing machinery is safe at all.
"""

from __future__ import annotations

import pytest

from topos.query.scope_arbiter import (
    ARB_CAPPED,
    ARB_HEAD_DISABLED,
    ARB_HEAD_ROUTED,
    ARB_HEAD_SILENT,
    ARB_HEAD_UNAVAILABLE,
    ARB_HEAD_UNCERTAIN,
    ARB_MODE_FLOORED,
    ARB_NO_PRIOR,
    ENV_HEAD_ROUTES,
    MODE_FLOOR,
    RoutingGates,
    RulePrior,
    SOURCE_HEAD,
    SOURCE_RULES,
    arbitrate,
    evaluate_promotion,
    head_routes_enabled,
    promotion_status,
)
from topos.query.scope_classifier import SOURCE_HEAD as VERDICT_SOURCE_HEAD
from topos.query.scope_classifier import ScopeVerdict
from topos.query.scope_shadow import KIND_ROUTE, KIND_TURN, routing_comparison


def _verdict(labels=(), *, confidence=0.9, escalated=False, reason=""):
    return ScopeVerdict(
        tuple(labels), confidence, VERDICT_SOURCE_HEAD, escalated, {}, reason=reason
    )


PRIORS = (
    RulePrior("schedule:read", "raw", rule_id="schedule_raw", priority=25),
    RulePrior("health:read", "summary", rule_id="health_wellbeing", priority=30),
)


# --- the flag ------------------------------------------------------------------------


def test_head_routing_is_off_by_default(monkeypatch) -> None:
    """The hard constraint. Nothing in this repo sets the flag, and an unset
    environment must leave routing exactly as the keyword rules produced it."""
    monkeypatch.delenv(ENV_HEAD_ROUTES, raising=False)
    assert head_routes_enabled() is False

    # And the default is honoured through the real entry point, not just the predicate:
    # a head that wants a completely different scope set changes nothing.
    decision = arbitrate(PRIORS, _verdict(["messages:read"]))
    assert decision.source == SOURCE_RULES
    assert decision.arbitration == ARB_HEAD_DISABLED
    assert decision.routes == (("schedule:read", "raw"), ("health:read", "summary"))


def test_flag_accepts_the_usual_truthy_spellings(monkeypatch) -> None:
    for value in ("1", "true", "yes", "ON"):
        monkeypatch.setenv(ENV_HEAD_ROUTES, value)
        assert head_routes_enabled() is True
    for value in ("", "0", "off", "no", "maybe"):
        monkeypatch.setenv(ENV_HEAD_ROUTES, value)
        assert head_routes_enabled() is False


def test_no_env_flag_is_set_anywhere_in_the_shipped_package() -> None:
    """A default-off flag that some module quietly turns on is not default-off."""
    from pathlib import Path

    import topos

    root = Path(topos.__file__).resolve().parent
    setters = [
        path
        for path in root.rglob("*.py")
        if path.name != "scope_arbiter.py"
        and "TOPOS_SCOPE_HEAD_ROUTES" in path.read_text("utf-8")
    ]
    assert setters == []


# --- arbitration: the rules keep the floor -------------------------------------------


def test_rules_decide_when_the_head_is_not_installed() -> None:
    decision = arbitrate(PRIORS, None, enabled=True)
    assert decision.source == SOURCE_RULES
    assert decision.arbitration == ARB_HEAD_UNAVAILABLE


@pytest.mark.parametrize("reason", ["ambiguity", "ignorance"])
def test_an_uncertain_head_falls_back_to_the_rules(reason) -> None:
    decision = arbitrate(PRIORS, _verdict(escalated=True, reason=reason), enabled=True)
    assert decision.source == SOURCE_RULES
    assert decision.arbitration == ARB_HEAD_UNCERTAIN
    assert decision.scopes == ("schedule:read", "health:read")


def test_a_confidently_silent_head_cannot_empty_a_routed_turn() -> None:
    """`confident-none` is the head's largest measured divergence from the rules on real
    traffic (59.6% of routed turns). Deferring to it would delete data from an answer
    with nothing in the response to say so."""
    decision = arbitrate(PRIORS, _verdict((), reason="confident-none"), enabled=True)
    assert decision.source == SOURCE_RULES
    assert decision.arbitration == ARB_HEAD_SILENT
    assert decision.scopes == ("schedule:read", "health:read")


def test_both_silent_routes_nothing() -> None:
    decision = arbitrate((), _verdict(()), enabled=True)
    assert decision.routes == ()


# --- arbitration: what the head is allowed to do -------------------------------------


def test_the_head_decides_the_set_and_inherits_the_rules_access_modes() -> None:
    decision = arbitrate(PRIORS, _verdict(["health:read"]), enabled=True)
    assert decision.source == SOURCE_HEAD
    assert decision.arbitration == ARB_HEAD_ROUTED
    # schedule:read dropped by the head; health keeps the mode its rule asked for.
    assert decision.routes == (("health:read", "summary"),)


def test_a_scope_no_rule_predicted_is_floored_to_summary() -> None:
    """Routing is not authorization. The head has no disclosure opinion and was never
    trained on one, so a scope it invents is queried at the narrowest mode."""
    decision = arbitrate(PRIORS, _verdict(["places:read"]), enabled=True)
    assert decision.routes == (("places:read", MODE_FLOOR),)
    assert ARB_NO_PRIOR in decision.notes
    assert ARB_MODE_FLOORED in decision.notes


def test_the_head_can_never_widen_an_access_mode() -> None:
    """Even where a rule asked for `summary`, the head naming that scope must not
    upgrade it — the head has no mode to upgrade it to."""
    priors = (RulePrior("schedule:read", "summary", priority=10),)
    decision = arbitrate(priors, _verdict(["schedule:read"]), enabled=True)
    assert decision.routes == (("schedule:read", "summary"),)


def test_agreed_scopes_keep_the_rules_ordering() -> None:
    """A promoted head must not silently reshuffle which lane lands first in the
    synthesis context; that is a behaviour change nobody asked for."""
    decision = arbitrate(
        PRIORS, _verdict(["health:read", "schedule:read", "places:read"]), enabled=True
    )
    assert decision.scopes == ("schedule:read", "health:read", "places:read")


def test_the_head_is_held_to_the_same_route_budget() -> None:
    decision = arbitrate(
        (),
        _verdict(["a:read", "b:read", "c:read", "d:read", "e:read"]),
        enabled=True,
        max_routes=4,
    )
    assert len(decision.routes) == 4
    assert ARB_CAPPED in decision.notes


def test_priors_merge_like_the_incumbent_router() -> None:
    """Lowest priority first, widest requested mode wins, duplicates collapse — the
    behaviour of `_merge_scope_routes`, so the flag-off path is not a second router."""
    priors = (
        RulePrior("schedule:read", "summary", priority=25),
        RulePrior("schedule:read", "raw", priority=18),
        RulePrior("health:read", "summary", priority=30),
    )
    decision = arbitrate(priors, None)
    assert decision.routes == (("schedule:read", "raw"), ("health:read", "summary"))


def test_decision_telemetry_carries_no_text() -> None:
    decision = arbitrate(PRIORS, _verdict(["health:read"]), enabled=True)
    telemetry = decision.as_telemetry()
    assert set(telemetry) == {"source", "arbitration", "notes", "scopes", "n_scopes"}
    assert all(isinstance(note, str) and " " not in note for note in telemetry["notes"])


# --- the gates -----------------------------------------------------------------------


def _rows(*specs, kind=KIND_ROUTE):
    """specs: (text, router_scope, predicted, verdict). One row per routed scope, as
    `observe()` writes them."""
    return [
        {
            "kind": kind,
            "text": text,
            "router_scope": router_scope,
            "predicted": list(predicted),
            "verdict": verdict,
            "confidence": 0.9,
            "reason": "",
        }
        for text, router_scope, predicted, verdict in specs
    ]


def test_routing_comparison_reassembles_multi_scope_turns() -> None:
    """The bug this function exists to avoid: a head that correctly names both scopes of
    a compound ask scores one `hit` and one `over` at row level, which reads as partial
    credit for a completely right answer."""
    rows = _rows(
        ("am I free friday", "availability:read", ["availability:read", "schedule:read"], "over"),
        ("am I free friday", "schedule:read", ["availability:read", "schedule:read"], "hit"),
    )
    comparison = routing_comparison(rows)
    assert comparison["turns"] == 1
    assert comparison["exact"] == 1.0
    assert comparison["dead_rate"] == 0.0


def test_routing_comparison_ignores_turn_kind_rows() -> None:
    rows = _rows(("q", "health:read", ["health:read"], "hit"))
    rows += _rows(("q2", "", [], "abstain"), kind=KIND_TURN)
    assert routing_comparison(rows)["turns"] == 1


def test_routing_comparison_is_text_free() -> None:
    rows = _rows(("zzqx-what-did-my-therapist-say", "health:read", [], "abstain"))
    assert "zzqx" not in repr(routing_comparison(rows))


def test_unmeasurable_rates_are_none_not_zero() -> None:
    """`0.0` and "never observed" print identically, and a gate that cannot tell them
    apart passes on an empty log."""
    comparison = routing_comparison([])
    assert comparison["exact"] is None
    assert comparison["disjoint_rate"] is None


def test_an_empty_log_fails_the_gate_rather_than_passing_it() -> None:
    failures = evaluate_promotion(routing_comparison([]))
    assert failures
    assert any("unmeasurable" in f for f in failures)


def test_the_gate_is_a_conjunction_not_an_average() -> None:
    """A head with a spotless exact rate still fails on a dead rate that would empty
    lanes — the point of reading every clause instead of their mean."""
    comparison = {
        "turns": 1000,
        "exact": 0.95,
        "dead_rate": 0.30,
        "disjoint_rate": 0.0,
        "escalation_rate": 0.0,
        "scope_turns": {},
        "per_scope_recall": {},
    }
    failures = evaluate_promotion(comparison, RoutingGates(min_turns_per_scope=0))
    assert any("dead_rate" in f for f in failures)


def test_a_thin_scope_fails_even_when_the_headline_is_clean() -> None:
    comparison = {
        "turns": 1000,
        "exact": 0.95,
        "dead_rate": 0.0,
        "disjoint_rate": 0.0,
        "escalation_rate": 0.0,
        "scope_turns": {"schedule:read": 980, "places:read": 3},
        "per_scope_recall": {"schedule:read": 0.99, "places:read": 1.0},
    }
    failures = evaluate_promotion(comparison)
    assert any("places:read" in f for f in failures)


def test_promotion_status_on_the_real_shape_of_the_log_is_not_ready() -> None:
    """A head that goes dead on most routed turns must never come back `ready`. This is
    the shape of this node's actual captured traffic, not a hypothetical."""
    rows = _rows(
        *[(f"q{i}", "activity:read", [], "abstain") for i in range(20)],
        *[(f"r{i}", "schedule:read", ["schedule:read"], "hit") for i in range(5)],
    )
    status = promotion_status(rows)
    assert status.ready is False
    assert any("dead_rate" in f for f in status.failures)
    assert status.as_telemetry()["head_routes_enabled"] is False
