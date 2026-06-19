"""GT-EN-P3-S2-01: QuerySessionStore lifecycle."""

import pytest

from helpers import make_adapter_bundle

pytestmark = pytest.mark.gap


def test_session_create_append_get_purge() -> None:
    store = make_adapter_bundle().query_session
    session_id = store.put(
        {
            "session_id": "qs_lifecycle",
            "requester_id": "owner",
            "intent_hash": "abc",
            "envelope_json": {"scopes": ["availability:read"]},
            "ttl_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    store.append_artifact(
        session_id,
        {
            "cache_key": "availability:read:inference:abc",
            "public_result_json": {"answer": "yes", "confidence": 0.5},
            "retrieval_fingerprint": "fp1",
            "game_layer_strategy": "inference_gated",
        },
    )
    loaded = store.get(session_id)
    assert loaded is not None
    assert len(loaded.get("artifacts") or []) == 1
    purged = store.purge_expired()
    assert purged >= 0
