"""
Gap: messages:read raw query empty post sync
PRD: PRD_01
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from remediation_helpers import adapter_bundle, messages_manifest, sqlite_conn

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_messenger_raw_scope_returns_rows() -> None:
    conn = sqlite_conn()
    bundle = adapter_bundle(conn)
    bundle.canonical.upsert(
        "conversation_messages",
        {
            "message_id": "im-1",
            "conversation_id": "c1",
            "sender_type": "contact",
            "sender_id": "+15551212",
            "event_at": "2026-06-01T00:00:00Z",
            "content": "hello",
            "source_id": "imessage",
        },
    )
    orch = QueryPipelineOrchestrator(adapters=bundle)
    out = await orch.execute(
        query_text="hello",
        scope_id="messages:read",
        access_mode="raw",
        manifest=messages_manifest(),
        query_session_id="gt-r01-04",
    )
    rows = (out.get("public_result") or {}).get("rows") or []
    assert len(rows) >= 1
