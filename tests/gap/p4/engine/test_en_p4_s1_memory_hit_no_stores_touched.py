"""
Gap: Session memory — re-query on turn 2 → memory_hit with empty stores_touched
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.session import TurnOutcome

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_memory_hit_has_empty_stores_touched() -> None:
    bundle = make_adapter_bundle()
    orch = QueryPipelineOrchestrator(adapters=bundle)
    manifest = availability_manifest()
    query = "Am I free Thursday?"
    session_id = "qs_p4_mem_hit"

    first = await orch.execute(
        query_text=query,
        scope_id="availability:read",
        access_mode="inference",
        manifest=manifest,
        query_session_id=session_id,
    )
    assert first["turn_outcome"] == TurnOutcome.LIVE_QUERY.value

    second = await orch.execute(
        query_text=query,
        scope_id="availability:read",
        access_mode="inference",
        manifest=manifest,
        query_session_id=session_id,
    )
    assert second["turn_outcome"] == TurnOutcome.MEMORY_HIT.value
    assert second["audit"]["stores_touched"] == []
