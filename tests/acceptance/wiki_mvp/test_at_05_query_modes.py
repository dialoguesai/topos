"""
AT-5: Raw/Summary/Inference query modes for three scope/mode pairs (engine harness).
Profile: local
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from helpers import availability_manifest, make_adapter_bundle, messages_manifest, relationship_manifest

pytestmark = pytest.mark.acceptance


@pytest.mark.asyncio
async def test_at_05_three_scope_mode_pairs() -> None:
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    cases = [
        ("messages:read", "raw", messages_manifest(), "recent messages"),
        ("relationship_context:read", "summary", relationship_manifest(), "relationship"),
        ("availability:read", "inference", availability_manifest(), "free thursday"),
    ]
    for scope_id, mode, manifest, query in cases:
        out = await orch.execute(
            query_text=query,
            scope_id=scope_id,
            access_mode=mode,
            manifest=manifest,
            query_session_id=f"at5-{scope_id}",
        )
        assert out.get("public_result") is not None
        assert out.get("turn_outcome") in ("live_query", "memory_hit", "expand_boundary", "requalify")
