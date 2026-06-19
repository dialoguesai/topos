"""GT-EN-P3-S2-02c: Stale fingerprint triggers live_query."""

import pytest

from topos.query.fingerprint import compute_retrieval_fingerprint
from topos.query.intent import compute_intent_hash
from topos.query.session import QueryArtifact, QuerySession
from topos.query.session_utils import build_cache_key
from topos.query.turn_classifier import TurnClassifierLite
from topos.query.types import QueryTurn

pytestmark = pytest.mark.gap


def test_stale_fingerprint_falls_back_to_live_query() -> None:
    scope = "availability:read"
    mode = "inference"
    query = "Am I free Thursday?"
    intent_hash = compute_intent_hash(scope_id=scope, access_mode=mode, query_text=query)
    cache_key = build_cache_key(scope_id=scope, access_mode=mode, intent_hash=intent_hash)
    session = QuerySession(
        session_id="s1",
        requester_id="owner",
        intent_hash=intent_hash,
        envelope_json={"scopes": [scope], "access_modes": [mode]},
        artifacts=[
            QueryArtifact(
                artifact_id="a1",
                session_id="s1",
                cache_key=cache_key,
                retrieval_fingerprint="stale-fingerprint",
            )
        ],
    )
    result = TurnClassifierLite().classify(
        QueryTurn(query_text=query, scope_id=scope, access_mode=mode, intent_hash=intent_hash),
        session,
    )
    assert result.outcome.value == "live_query"
    expected = compute_retrieval_fingerprint(scope_id=scope, access_mode=mode)
    assert expected != "stale-fingerprint"
