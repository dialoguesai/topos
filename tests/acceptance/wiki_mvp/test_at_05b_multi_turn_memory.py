"""
AT-5b: Multi-turn memory_hit with empty stores_touched on turn 2.
Profile: local
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.session import TurnOutcome

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.acceptance


@pytest.mark.asyncio
async def test_at_05b_multi_turn_memory_hit() -> None:
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    sid = "at-5b-session"
    q = "Am I free Thursday afternoon?"
    await orch.execute(
        query_text=q,
        scope_id="availability:read",
        access_mode="inference",
        manifest=availability_manifest(),
        query_session_id=sid,
    )
    second = await orch.execute(
        query_text=q,
        scope_id="availability:read",
        access_mode="inference",
        manifest=availability_manifest(),
        query_session_id=sid,
    )
    assert second["turn_outcome"] == TurnOutcome.MEMORY_HIT.value
    assert second["audit"]["stores_touched"] == []
