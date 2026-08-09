"""Tests for VoxTerm transcript ui_stream ingest → conversation_messages."""

from __future__ import annotations

import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
from topos.sources.registry import VOXTERM_TRANSCRIPTS


@pytest.fixture
def migrated_conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "voxterm.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_voxterm_transcript_ui_stream_writes_conversation_message(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    payload = {
        "message_id": "transcript-2026-06-24-1430-voxterm:0:0",
        "conversation_id": "transcript-2026-06-24-1430-voxterm",
        "sender_id": "Speaker 1",
        "sender_type": "human",
        "content": "Hello world",
        "event_at": "2026-06-24T14:30:05Z",
        "origin_device": "550e8400-e29b-41d4-a716-446655440000",
        "batch_index": 0,
        "segment_index": 0,
    }

    result = await _ingest_ui_payload_direct(
        dataset_id="user:default:device",
        schema_id="voxterm.transcript.v1",
        payload=payload,
        job_id="job-voxterm-1",
        source_id=VOXTERM_TRANSCRIPTS.source_id,
    )

    assert result["status"] == "ok"
    assert result["records_processed"] == 1

    row = migrated_conn.execute(
        """
        SELECT message_id, conversation_id, sender_id, content, source_id
        FROM conversation_messages
        WHERE message_id=?
        """,
        (payload["message_id"],),
    ).fetchone()
    assert row is not None
    assert row["conversation_id"] == payload["conversation_id"]
    assert row["sender_id"] == "Speaker 1"
    assert row["content"] == "Hello world"
    assert row["source_id"] == "voxterm_transcripts"
