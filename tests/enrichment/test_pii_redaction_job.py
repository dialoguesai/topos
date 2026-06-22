"""Tests for ingest-time PiiRedactionJob."""

import sqlite3
from unittest.mock import patch

import pytest

from topos.enrichment.jobs.canonical.pii_redaction_job import PiiRedactionJob


@pytest.mark.asyncio
async def test_pii_redaction_job_writes_disclosure_and_mutates_batch():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            content TEXT,
            source_id TEXT,
            content_disclosure TEXT,
            content_disclosure_hash TEXT,
            content_disclosure_model TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO conversation_messages (message_id, conversation_id, content, source_id)
        VALUES ('m1', 'c1', 'Contact alice@example.com', 'imessage')
        """
    )
    conn.commit()

    job = PiiRedactionJob()
    messages = [
        {
            "_table": "conversation_messages",
            "message_id": "m1",
            "content": "Contact alice@example.com",
            "source_id": "imessage",
        }
    ]

    with patch("topos.sanitization.privacy_filter.privacy_filter_enabled", return_value=True):
        with patch(
            "topos.sanitization.privacy_filter.apply_text_transform_with_privacy_filter",
            return_value="Contact [EMAIL]",
        ):
            with patch("topos.core.state.get_db_connection", return_value=conn):
                result = await job.enrich(messages)

    assert result
    assert messages[0]["content"] == "Contact [EMAIL]"
    row = conn.execute(
        "SELECT content, content_disclosure FROM conversation_messages WHERE message_id='m1'"
    ).fetchone()
    assert row[0] == "Contact alice@example.com"
    assert row[1] == "Contact [EMAIL]"


@pytest.mark.asyncio
async def test_pii_redaction_job_skips_when_privacy_filter_disabled():
    job = PiiRedactionJob()
    messages = [{"_table": "conversation_messages", "message_id": "m1", "content": "hello"}]
    with patch("topos.sanitization.privacy_filter.privacy_filter_enabled", return_value=False):
        result = await job.enrich(messages)
    assert result == []
    assert messages[0]["content"] == "hello"
