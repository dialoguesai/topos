"""QuerySession and QueryArtifact JSON shapes (PRD §8.4)."""

from topos.query.session import QueryArtifact, QuerySession


def test_query_session_artifact_shape() -> None:
    artifact = QueryArtifact(
        artifact_id="art-1",
        session_id="sess-1",
        cache_key="ck-1",
        public_result_json={"rows": []},
        retrieval_fingerprint="fp-abc",
        game_layer_strategy="direct",
        created_at="2026-06-19T00:00:00Z",
    )
    session = QuerySession(
        session_id="sess-1",
        requester_id="user-1",
        intent_hash="intent-hash",
        envelope_json={"scope_id": "messages:read"},
        ttl_expires_at="2026-06-20T00:00:00Z",
        artifacts=[artifact],
    )
    payload = session.to_dict()
    assert payload["session_id"] == "sess-1"
    assert payload["requester_id"] == "user-1"
    assert payload["artifacts"][0]["artifact_id"] == "art-1"
    assert payload["artifacts"][0]["game_layer_strategy"] == "direct"
