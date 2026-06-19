"""GT-EN-P3-S2-01b: Session envelope scopes and modes."""

import pytest

from topos.query.turn_classifier import TurnClassifierLite
from topos.query.types import QueryTurn

from helpers import make_adapter_bundle

pytestmark = pytest.mark.gap


def test_envelope_blocks_out_of_scope_turn() -> None:
    store = make_adapter_bundle().query_session
    session_id = "qs_env"
    store.put(
        {
            "session_id": session_id,
            "requester_id": "owner",
            "intent_hash": "h1",
            "envelope_json": {"scopes": ["availability:read"], "access_modes": ["inference"]},
        }
    )
    session_data = store.get(session_id)
    from topos.query.session import QuerySession

    session = QuerySession(
        session_id=session_id,
        requester_id="owner",
        intent_hash="h1",
        envelope_json=session_data["envelope_json"],
    )
    result = TurnClassifierLite().classify(
        QueryTurn(
            query_text="show messages",
            scope_id="messages:read",
            access_mode="raw",
            intent_hash="h2",
        ),
        session,
    )
    assert result.outcome.value == "expand_boundary"
