"""
Gap: Unified canonical storage — ingest writes != query reads
PRD: PRD_01
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from remediation_helpers import adapter_bundle, ai_conversations_manifest, ingest_chatgpt_message, sqlite_conn

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_chatgpt_ingest_query_raw_returns_rows() -> None:
    conn = sqlite_conn()
    ingest_chatgpt_message(conn, message_id="msg-r01-01")
    orch = QueryPipelineOrchestrator(adapters=adapter_bundle(conn))
    out = await orch.execute(
        query_text="investor",
        scope_id="ai_conversations:read",
        access_mode="raw",
        manifest=ai_conversations_manifest(),
        query_session_id="gt-r01-01",
    )
    rows = (out.get("public_result") or {}).get("rows") or []
    assert out["turn_outcome"] == "live_query"
    assert len(rows) >= 1
    assert any(str(r.get("record_id") or r.get("message_id")) == "msg-r01-01" for r in rows)
