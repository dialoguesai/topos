"""
Gap: Filters — partial coverage → all Raw paths filtered + audited
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from helpers import make_adapter_bundle, messages_manifest

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_raw_pipeline_audit_records_filters() -> None:
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    out = await orch.execute(
        query_text="show messages",
        scope_id="messages:read",
        access_mode="raw",
        manifest=messages_manifest(),
        query_session_id="filter-audit",
    )
    audit = out.get("audit") or {}
    assert "filters_applied" in audit
    assert isinstance(audit["filters_applied"], list)
