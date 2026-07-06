"""Tests for the narrow_request response contract (plan §C.1)."""

from __future__ import annotations

import pytest

from topos.query.negotiation import build_narrow_request_response, qualify_intent
from topos.query.session import TurnOutcome

pytestmark = [pytest.mark.private]


def test_turn_outcome_enum_has_narrow_request():
    assert TurnOutcome.NARROW_REQUEST.value == "narrow_request"


def test_narrow_request_response_shape():
    out = qualify_intent(
        scope_id="messages:read", access_mode="summary", query_text="everything", grant_ceiling="summary"
    )
    resp = build_narrow_request_response(
        offer=out.offer, scope_id="messages:read", access_mode="summary", session_id="qs_1"
    )
    assert resp["turn_outcome"] == "narrow_request"
    # A narrow_request never discloses anything.
    assert resp["public_result"] is None
    assert resp["reason"] == out.offer.reason
    offer = resp["offer"]
    assert offer["round"] == 1 and offer["max_rounds"] == out.offer.max_rounds
    assert offer["suggested_intents"]
    assert set(offer["access_modes"]) == {"summary", "inference", "raw"}
    assert resp["session_id"] == "qs_1" == resp["query_session_id"]


def test_offer_to_dict_is_json_safe():
    import json

    out = qualify_intent(
        scope_id="availability:read", access_mode="raw", query_text="find slot", grant_ceiling="summary"
    )
    # round-trips through JSON without error
    blob = json.dumps(out.offer.to_dict())
    assert "requires" in blob and "reason" in blob
