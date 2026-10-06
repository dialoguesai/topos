"""A stale older shared-query relay cannot invoke the node query pipeline."""

import pytest

from topos.core.handlers.query import handle_query
from topos.query.pipeline import QueryPipelineOrchestrator


@pytest.mark.asyncio
async def test_older_grantee_query_refused_before_pipeline(monkeypatch):
    def forbidden_db():
        raise AssertionError("retired query reached the database")

    monkeypatch.setattr("topos.core.handlers.get_db_connection", forbidden_db)
    response = await handle_query({
        "id": "retired-read",
        "type": "query",
        "payload": {
            "is_grantee_request": True,
            "scope_id": "messages:read",
            "access_mode": "raw",
            "intent": "What did the owner say?",
        },
    })
    assert response == {"id": "retired-read", "status": "error", "error": "retired_grantee_query"}


@pytest.mark.asyncio
async def test_internal_orchestrator_refuses_retired_grantee_before_adapters():
    orchestrator = object.__new__(QueryPipelineOrchestrator)
    result = await orchestrator.execute(
        query_text="What did the owner say?", scope_id="messages:read",
        access_mode="raw", manifest=None, is_grantee_request=True,
        query_session_id="retired-internal",
    )
    assert result["turn_outcome"] == "denied"
    assert result["deny_reason"] == "retired_grantee_query"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    {"payload": {"is_grantee_request": True}},
    {"payload": {"is_grantee_request": "true"}},
    {"payload": {"is_grantee_request": 1}},
    {"payload": {}, "caller": {"is_grantee_request": True}},          # the block the control plane stamps
    {"payload": {"is_grantee_request": False}, "caller": {"is_grantee_request": True}},
    {"payload": ["not", "a", "payload"]},
])
async def test_every_way_a_control_plane_says_grantee_is_refused_before_the_pipeline(monkeypatch, message):
    def forbidden_db():
        raise AssertionError("retired query reached the database")

    monkeypatch.setattr("topos.core.handlers.get_db_connection", forbidden_db)
    monkeypatch.setattr("topos.core.handlers.query.get_query_orchestrator",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("retired query reached the pipeline")),
                        raising=False)
    response = await handle_query({"id": "retired-read", "type": "query", **message})
    assert response == {"id": "retired-read", "status": "error", "error": "retired_grantee_query"}
