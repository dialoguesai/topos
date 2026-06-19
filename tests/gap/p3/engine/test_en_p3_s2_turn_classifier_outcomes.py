"""GT-EN-P3-S2-02: TurnClassifier five outcomes."""

import pytest

from topos.query.fingerprint import compute_retrieval_fingerprint
from topos.query.intent import compute_intent_hash
from topos.query.session import QueryArtifact, QuerySession, TurnOutcome
from topos.query.session_utils import build_cache_key
from topos.query.turn_classifier import TurnClassifierLite
from topos.query.types import QueryTurn

pytestmark = pytest.mark.gap


def test_classifier_returns_all_outcome_types() -> None:
    classifier = TurnClassifierLite()
    scope = "availability:read"
    mode = "inference"
    query = "Am I free Thursday?"
    intent_hash = compute_intent_hash(scope_id=scope, access_mode=mode, query_text=query)
    cache_key = build_cache_key(scope_id=scope, access_mode=mode, intent_hash=intent_hash)
    fp = compute_retrieval_fingerprint(scope_id=scope, access_mode=mode)

    outcomes = {
        classifier.classify(
            QueryTurn(query_text=query, scope_id="", access_mode=mode),
            None,
        ).outcome,
        classifier.classify(
            QueryTurn(query_text=query, scope_id=scope, access_mode=mode, intent_hash=intent_hash),
            None,
        ).outcome,
        classifier.classify(
            QueryTurn(query_text=query, scope_id=scope, access_mode=mode, intent_hash=intent_hash),
            QuerySession(
                session_id="s1",
                requester_id="owner",
                intent_hash=intent_hash,
                envelope_json={"scopes": [scope], "access_modes": [mode]},
                artifacts=[
                    QueryArtifact(
                        artifact_id="a1",
                        session_id="s1",
                        cache_key=cache_key,
                        retrieval_fingerprint=fp,
                    )
                ],
            ),
        ).outcome,
        classifier.classify(
            QueryTurn(query_text=query, scope_id="messages:read", access_mode=mode, intent_hash=intent_hash),
            QuerySession(
                session_id="s1",
                requester_id="owner",
                intent_hash=intent_hash,
                envelope_json={"scopes": [scope], "access_modes": [mode]},
            ),
        ).outcome,
        classifier.classify(
            QueryTurn(
                query_text="show my messages",
                scope_id="messages:read",
                access_mode=mode,
                intent_hash=compute_intent_hash(
                    scope_id="messages:read", access_mode=mode, query_text="show my messages"
                ),
            ),
            QuerySession(
                session_id="s1",
                requester_id="owner",
                intent_hash=intent_hash,
                envelope_json={
                    "scopes": [scope, "messages:read"],
                    "access_modes": [mode],
                    "last_scope_id": scope,
                },
            ),
        ).outcome,
    }
    assert TurnOutcome.DENIED in outcomes
    assert TurnOutcome.LIVE_QUERY in outcomes
    assert TurnOutcome.MEMORY_HIT in outcomes
    assert TurnOutcome.EXPAND_BOUNDARY in outcomes
    assert TurnOutcome.REQUALIFY in outcomes
