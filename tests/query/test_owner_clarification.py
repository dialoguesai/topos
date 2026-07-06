"""§C.3 — owner-side clarification loop tests.

When a request needs owner approval, the owner's AI gets a clarification with a recommended
MINIMAL-disclosure default. The owner accepts / broadens / denies; recommendation-acceptance
is tracked.
"""

from __future__ import annotations

import pytest

from topos.query.negotiation import (
    build_owner_clarification,
    qualify_intent,
    recommendation_acceptance_rate,
    resolve_owner_decision,
)

pytestmark = [pytest.mark.private]


def _offer(scope="availability:read", mode="raw", ceiling="summary", intent="find a slot"):
    out = qualify_intent(scope_id=scope, access_mode=mode, query_text=intent, grant_ceiling=ceiling)
    assert out.offer is not None
    return out.offer


def test_recommendation_is_minimal_proportional():
    clar = build_owner_clarification(offer=_offer(), requester_id="acme-agent")
    rec = clar.recommended
    # Lowest available mode (summary), the granted scope, and a bounded window for availability.
    assert rec.access_mode == "summary"
    assert rec.scope_id == "availability:read"
    assert rec.time_window_days == 30
    assert "acme-agent" in clar.question and "summary" in clar.question


def test_options_include_recommended_broader_and_deny():
    clar = build_owner_clarification(offer=_offer(), requester_id="req")
    choices = [o["choice"] for o in clar.options]
    assert choices[0] == "accept_recommended"
    assert "deny" in choices
    assert "broader" in choices  # a mode above summary exists


def test_resolve_accept_recommended_yields_minimal_grant():
    clar = build_owner_clarification(offer=_offer(), requester_id="req")
    eff = resolve_owner_decision(clar, "accept_recommended")
    assert eff["access_mode"] == "summary"
    assert eff["time_window_days"] == 30


def test_resolve_broader_yields_higher_mode():
    clar = build_owner_clarification(offer=_offer(), requester_id="req")
    eff = resolve_owner_decision(clar, "broader")
    assert eff["access_mode"] in ("inference", "raw")


def test_resolve_deny_returns_none():
    clar = build_owner_clarification(offer=_offer(), requester_id="req")
    assert resolve_owner_decision(clar, "deny") is None
    # Unknown choice fails closed to deny.
    assert resolve_owner_decision(clar, "give_everything") is None


def test_recommendation_acceptance_rate():
    decisions = ["accept_recommended", "accept_recommended", "broader", "deny"]
    assert recommendation_acceptance_rate(decisions) == 0.5
    assert recommendation_acceptance_rate([]) == 0.0


def test_purpose_missing_clarification_has_no_time_window():
    # A non-time-bounded scope with a broad intent → recommendation without a window.
    offer = _offer(scope="relationship_context:read", mode="summary", ceiling="summary", intent="everything")
    clar = build_owner_clarification(offer=offer, requester_id="req")
    assert clar.recommended.time_window_days is None
    assert clar.recommended.access_mode == "summary"
