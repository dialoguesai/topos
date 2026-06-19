"""GT-EN-P3-S2-02d: New intent hash → live_query."""

import pytest

from topos.query.intent import compute_intent_hash
from topos.query.session import QuerySession
from topos.query.turn_classifier import TurnClassifierLite
from topos.query.types import QueryTurn

pytestmark = pytest.mark.gap


def test_new_intent_hash_is_live_query_not_memory_hit() -> None:
    scope = "availability:read"
    mode = "inference"
    session = QuerySession(
        session_id="s1",
        requester_id="owner",
        intent_hash=compute_intent_hash(scope_id=scope, access_mode=mode, query_text="first question"),
        envelope_json={"scopes": [scope], "access_modes": [mode]},
        artifacts=[],
    )
    result = TurnClassifierLite().classify(
        QueryTurn(
            query_text="what about Friday instead",
            scope_id=scope,
            access_mode=mode,
            intent_hash=compute_intent_hash(
                scope_id=scope, access_mode=mode, query_text="what about Friday instead"
            ),
        ),
        session,
    )
    assert result.outcome.value == "live_query"
