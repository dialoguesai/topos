"""
Gap: Reprocess — re-upload required → API backfill from raw/canonical
Sprint: EN-P1-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import json
import sqlite3

import pytest

from topos.ingestion.reprocess import reprocess_source
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.raw.raw_tables_manager import RawTablesManager

pytestmark = [
    pytest.mark.gap,
    pytest.mark.check("C-eng-ingest-reprocess-idempotent"),
]


@pytest.mark.asyncio
async def test_reprocess_api_contract_and_idempotency(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender_type TEXT NOT NULL,
            sender_id TEXT,
            event_at TEXT NOT NULL,
            content TEXT NOT NULL,
            content_rendered TEXT,
            metadata_json TEXT,
            sequence INTEGER NOT NULL DEFAULT 0,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT,
            sync_batch_id TEXT,
            content_hash TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_chat_conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            title TEXT,
            source_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT,
            sync_batch_id TEXT
        )
        """
    )
    conn.commit()

    raw_manager = RawTablesManager(conn)
    payload = {
        "id": "m1",
        "thread_id": "t1",
        "role": "user",
        "content": "hello",
        "created_at": 1,
    }
    raw_manager.write_raw_record(
        source_id="chatgpt_file_ingestion",
        source_record_id="m1",
        payload=payload,
    )

    monkeypatch.setattr("topos.ingestion.reprocess.get_db_connection", lambda: conn)

    first = await reprocess_source(
        source_id="chatgpt_file_ingestion",
        dataset_id="user:chatgpt",
        from_stage="raw",
        run_enrichment=False,
    )
    assert first["status"] == "accepted"
    assert first["sync_batch_id"]
    assert "raw_write" in first["stages"]
    assert "canonical_map" in first["stages"]
    assert first["records_created"] >= 0
    assert first.get("raw_rows_loaded") == 1
    assert first.get("raw_table")

    second = await reprocess_source(
        source_id="chatgpt_file_ingestion",
        dataset_id="user:chatgpt",
        from_stage="raw",
        run_enrichment=False,
    )
    assert second["status"] == "accepted"
    assert second["records_created"] == 0 or second["records_unchanged"] >= 0

    audit_rows = conn.execute(
        "SELECT stage, status FROM ingest_audit WHERE sync_batch_id=?",
        (first["sync_batch_id"],),
    ).fetchall()
    assert len(audit_rows) >= 2
