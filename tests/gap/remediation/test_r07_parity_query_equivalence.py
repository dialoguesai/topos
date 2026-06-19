"""Gap: parity query equivalence — PRD_07"""
import pytest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import ai_conversations_manifest, ingest_chatgpt_message, sqlite_conn
pytestmark = pytest.mark.gap

@pytest.mark.asyncio
async def test_local_and_hosted_query_shape_match() -> None:
    conn = sqlite_conn()
    ingest_chatgpt_message(conn)
    local = QueryPipelineOrchestrator(adapters=AdapterFactory.create("local_database", conn=conn))
    hosted = QueryPipelineOrchestrator(adapters=AdapterFactory.create("hosted_database", conn=conn))
    kwargs = dict(query_text="investor", scope_id="ai_conversations:read", access_mode="raw", manifest=ai_conversations_manifest(), query_session_id="parity")
    local_out = await local.execute(**{**kwargs, "query_session_id": "parity-local"})
    hosted_out = await hosted.execute(**{**kwargs, "query_session_id": "parity-hosted"})
    assert set(local_out.keys()) == set(hosted_out.keys())
    assert local_out["turn_outcome"] == hosted_out["turn_outcome"]
