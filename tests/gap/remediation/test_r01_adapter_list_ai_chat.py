"""
Gap: Adapter list ai_chat_messages empty post-ingest
PRD: PRD_01
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from remediation_helpers import adapter_bundle, ingest_chatgpt_message, sqlite_conn

pytestmark = pytest.mark.gap


def test_adapter_list_ai_chat_non_empty() -> None:
    conn = sqlite_conn()
    ingest_chatgpt_message(conn)
    page = adapter_bundle(conn).canonical.list("ai_chat_messages", source_id="chatgpt_file_ingestion")
    assert page.total >= 1
    assert page.items
