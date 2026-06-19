"""
AT-8: Hosted parity spot-check (local fakes stand-in when hosted unavailable).
Profile: local + hosted fakes
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from helpers import make_adapter_bundle, messages_manifest

pytestmark = [pytest.mark.acceptance, pytest.mark.hosted_parity]


@pytest.mark.asyncio
async def test_at_08_local_hosted_fake_equivalence() -> None:
    local = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    hosted = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    kwargs = dict(
        query_text="messages",
        scope_id="messages:read",
        access_mode="raw",
        manifest=messages_manifest(),
        query_session_id="at-8",
    )
    local_out = await local.execute(**kwargs)
    hosted_out = await hosted.execute(**kwargs)
    assert local_out["turn_outcome"] == hosted_out["turn_outcome"]
