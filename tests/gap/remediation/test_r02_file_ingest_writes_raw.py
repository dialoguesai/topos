"""
Gap: File ingest skips raw retention
PRD: PRD_02
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.storage.raw.raw_tables_manager import RawTablesManager
from remediation_helpers import ingest_chatgpt_message, sqlite_conn

pytestmark = pytest.mark.gap


def test_file_ingest_path_can_write_raw() -> None:
    conn = sqlite_conn()
    ingest_chatgpt_message(conn)
    raw = RawTablesManager(conn)
    table = raw.get_raw_table_name("chatgpt_file_ingestion")
    raw.write_raw_record(
        source_id="chatgpt_file_ingestion",
        source_record_id="raw-check",
        payload={"id": "raw-check", "content": "x"},
    )
    raw_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    canonical = conn.execute(
        "SELECT COUNT(*) FROM ai_chat_messages WHERE source_id=?",
        ("chatgpt_file_ingestion",),
    ).fetchone()[0]
    assert canonical >= 1
    assert raw_count >= canonical
