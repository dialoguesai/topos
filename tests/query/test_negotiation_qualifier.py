"""Unit tests for the pre-retrieval intent qualifier (plan §C.1/§C.2).

Deterministic, no user data. Drives the exact narrow-vs-proceed semantics and the counter-
offer shape.
"""

from __future__ import annotations

import pytest

from topos.query.negotiation import (
    DEFAULT_MAX_ROUNDS,
    REASON_INTENT_TOO_BROAD,
    REASON_MODE_ABOVE_CEILING,
    REASON_PURPOSE_MISSING,
    REASON_TIME_WINDOW_REQUIRED,
    qualify_intent,
)

pytestmark = [pytest.mark.private]


def test_specific_bounded_intent_passes():
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="summary",
        query_text="Summarize messages with Alex about the Q3 launch in the last 30 days",
        grant_ceiling="summary",
    )
    assert out.ok is True
    assert out.offer is None


def test_empty_intent_requires_purpose():
    out = qualify_intent(scope_id="messages:read", access_mode="summary", query_text="   ", grant_ceiling="raw")
    assert out.ok is False
    assert out.offer.reason == REASON_PURPOSE_MISSING
    assert any(r["type"] == "purpose_statement" for r in out.offer.requires)


def test_broad_intent_is_narrowed():
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="summary",
        query_text="give me everything you have",
        grant_ceiling="summary",
    )
    assert out.ok is False
    assert out.offer.reason == REASON_INTENT_TOO_BROAD
    assert any(r["type"] == "narrower_topic" for r in out.offer.requires)
    assert out.offer.suggested_intents  # concrete templates offered


def test_too_short_intent_is_narrowed():
    out = qualify_intent(
        scope_id="relationship_context:read",
        access_mode="summary",
        query_text="data",
        grant_ceiling="summary",
    )
    assert out.ok is False
    assert out.offer.reason == REASON_INTENT_TOO_BROAD


def test_mode_above_ceiling_offers_lower_mode():
    out = qualify_intent(
        scope_id="relationship_context:read",  # not time-bounded, so mode is the only issue
        access_mode="raw",
        query_text="How is the requester connected to Maya Chen after our last meeting",
        grant_ceiling="summary",
    )
    assert out.ok is False
    assert out.offer.reason == REASON_MODE_ABOVE_CEILING
    assert out.offer.access_modes["raw"] == "requires_owner_approval"
    assert out.offer.access_modes["summary"] == "available"
    lower = next(r for r in out.offer.requires if r["type"] == "lower_mode")
    assert "summary" in lower["available_modes"]
    assert "raw" not in lower["available_modes"]


def test_time_bounded_scope_requires_window_when_unbounded():
    out = qualify_intent(
        scope_id="availability:read",
        access_mode="inference",
        query_text="find a meeting slot",
        grant_ceiling="inference",
    )
    assert out.ok is False
    assert out.offer.reason == REASON_TIME_WINDOW_REQUIRED
    tw = next(r for r in out.offer.requires if r["type"] == "time_window")
    assert tw["max_days"] == 30


def test_time_bound_satisfied_by_relative_phrase():
    out = qualify_intent(
        scope_id="availability:read",
        access_mode="inference",
        query_text="Is there a free 30-minute window next week",
        grant_ceiling="inference",
    )
    assert out.ok is True


def test_time_bound_satisfied_by_filter_manifest():
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="summary",
        query_text="messages with Alex about launch logistics",
        grant_ceiling="summary",
        filter_manifest={"filters": [{"filter_id": "rolling_window_days", "params": {"days": 14}}]},
    )
    assert out.ok is True


def test_mode_is_primary_reason_when_multiple_trip():
    # broad AND mode-above-ceiling AND unbounded → mode is the highest-priority reason.
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="raw",
        query_text="everything",
        grant_ceiling="summary",
    )
    assert out.ok is False
    assert out.offer.reason == REASON_MODE_ABOVE_CEILING
    # but every failing requirement is collected, not just the primary
    types = {r["type"] for r in out.offer.requires}
    assert {"lower_mode", "narrower_topic", "time_window"} <= types


def test_round_budget_exhaustion_signals_hard_deny():
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="summary",
        query_text="everything",
        grant_ceiling="summary",
        round=DEFAULT_MAX_ROUNDS + 1,
    )
    assert out.ok is False
    assert out.exhausted is True
    assert out.offer is None


def test_offer_carries_round_metadata():
    out = qualify_intent(
        scope_id="messages:read",
        access_mode="summary",
        query_text="everything",
        grant_ceiling="summary",
        round=2,
        max_rounds=4,
    )
    assert out.offer.round == 2
    assert out.offer.max_rounds == 4
