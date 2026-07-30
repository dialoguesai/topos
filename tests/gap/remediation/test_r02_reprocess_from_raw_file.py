"""
Gap: Reprocess from raw fails for file sources
PRD: PRD_02
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.ingestion.reprocess import reprocess_source
from topos.storage.raw.raw_tables_manager import RawTablesManager
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_reprocess_from_raw_restores_canonical(monkeypatch) -> None:
    conn = sqlite_conn()
    raw = RawTablesManager(conn)
    raw.write_raw_record(
        source_id="chatgpt_file_ingestion",
        source_record_id="m1",
        payload={"id": "m1", "thread_id": "t1", "role": "user", "content": "hello", "created_at": 1},
    )
    conn.execute("DELETE FROM ai_chat_messages")
    conn.commit()
    monkeypatch.setattr("topos.ingestion.reprocess.get_db_connection", lambda: conn)
    result = await reprocess_source(
        source_id="chatgpt_file_ingestion",
        dataset_id="user:chatgpt",
        from_stage="raw",
        run_enrichment=False,
    )
    assert result.get("records_created", 0) + result.get("records_updated", 0) >= 1
    row = conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0]
    assert row >= 1
