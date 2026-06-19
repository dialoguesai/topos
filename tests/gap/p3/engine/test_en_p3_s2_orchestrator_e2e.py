"""GT-EN-P3-S2-03: Orchestrator multi-turn e2e."""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.session import TurnOutcome

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_turn1_live_query_turn2_memory_hit() -> None:
    bundle = make_adapter_bundle()
    orch = QueryPipelineOrchestrator(adapters=bundle)
    manifest = availability_manifest()
    query = "Am I free Thursday afternoon?"
    session_id = "qs_e2e"

    turn1 = await orch.execute(
        query_text=query,
        scope_id="availability:read",
        access_mode="inference",
        manifest=manifest,
        query_session_id=session_id,
    )
    assert turn1["turn_outcome"] == TurnOutcome.LIVE_QUERY.value
    assert turn1.get("public_result") is not None

    turn2 = await orch.execute(
        query_text=query,
        scope_id="availability:read",
        access_mode="inference",
        manifest=manifest,
        query_session_id=session_id,
    )
    assert turn2["turn_outcome"] == TurnOutcome.MEMORY_HIT.value
    assert turn2["public_result"] == turn1["public_result"]
