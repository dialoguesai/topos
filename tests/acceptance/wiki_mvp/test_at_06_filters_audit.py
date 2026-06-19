"""
AT-6: PII/NSFW filters on Raw path (engine slice).
Profile: local
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from helpers import make_adapter_bundle, messages_manifest

pytestmark = pytest.mark.acceptance


@pytest.mark.asyncio
async def test_at_06_raw_query_records_filters_applied() -> None:
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    out = await orch.execute(
        query_text="show all messages",
        scope_id="messages:read",
        access_mode="raw",
        manifest=messages_manifest(),
        query_session_id="at-6",
    )
    assert "filters_applied" in out.get("audit", {})
