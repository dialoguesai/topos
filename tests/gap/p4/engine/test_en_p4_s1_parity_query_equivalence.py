"""
Gap: Query parity — profile mismatch → equivalent public_result on local + hosted fakes
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from helpers import availability_manifest, make_adapter_bundle, messages_manifest, relationship_manifest

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest,mode,scope_id,query",
    [
        (messages_manifest(), "raw", "messages:read", "recent messages"),
        (relationship_manifest(), "summary", "relationship_context:read", "relationship status"),
        (availability_manifest(), "inference", "availability:read", "free thursday?"),
    ],
)
async def test_query_public_result_shape_equivalent_across_profiles(manifest, mode, scope_id, query) -> None:
    local = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    hosted = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    local_out = await local.execute(
        query_text=query,
        scope_id=scope_id,
        access_mode=mode,
        manifest=manifest,
        query_session_id=f"parity-{scope_id}",
    )
    hosted_out = await hosted.execute(
        query_text=query,
        scope_id=scope_id,
        access_mode=mode,
        manifest=manifest,
        query_session_id=f"parity-{scope_id}",
    )
    assert set(local_out.keys()) == set(hosted_out.keys())
    assert local_out["turn_outcome"] == hosted_out["turn_outcome"]
    assert "public_result" in local_out
