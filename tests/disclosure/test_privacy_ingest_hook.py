"""Privacy ingest hook tests."""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from topos.disclosure.privacy_layer import run_privacy_disclosure_layer
from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up


@pytest.fixture
def memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            content TEXT,
            content_disclosure TEXT,
            content_disclosure_hash TEXT,
            content_disclosure_model TEXT,
            content_nsfw INTEGER DEFAULT 0,
            content_nsfw_score REAL,
            content_nsfw_model TEXT,
            source_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, content, source_id) VALUES (?,?,?,?)",
        ("m1", "c1", "Email alice@example.com", "src1"),
    )
    apply_canonical_disclosure_v1_up(conn)
    conn.commit()
    return conn


@pytest.mark.asyncio
async def test_privacy_ingest_hook_writes_disclosure(memory_conn):
    client = AsyncMock()
    client.redact_batch = AsyncMock(
        return_value={
            "items": [{"id": "conversation_messages:m1:content", "text": "Email [EMAIL]"}],
            "model": "openai/privacy-filter",
            "status": "ok",
        }
    )
    client.classify_nsfw_batch = AsyncMock(
        return_value={
            "items": [{"id": "conversation_messages:m1", "nsfw": False, "score": 0.01, "label": "safe"}],
            "model": "heuristic",
            "status": "ok",
        }
    )
    batch = [{"message_id": "m1", "content": "Email alice@example.com", "_table": "conversation_messages"}]
    result = await run_privacy_disclosure_layer(memory_conn, batch, client=client)
    assert result["records_updated"] == 1
    row = memory_conn.execute(
        "SELECT content_disclosure FROM conversation_messages WHERE message_id=?",
        ("m1",),
    ).fetchone()
    assert row[0] == "Email [EMAIL]"


def test_pii_redaction_not_in_canonical_jobs():
    from topos.enrichment.jobs import CANONICAL_JOBS
    from topos.sources.registry import CHATGPT_FILE, IMESSAGE, SIGNAL

    names = {job.get_job_name() for job in CANONICAL_JOBS}
    assert "pii_redaction" not in names
    for source in (CHATGPT_FILE, IMESSAGE, SIGNAL):
        jobs = list(getattr(source, "canonical_enrichment_jobs", []) or [])
        assert "pii_redaction" not in jobs


@pytest.mark.asyncio
async def test_a_record_the_model_failed_on_keeps_no_disclosure_and_grantee_reads_fail_closed(memory_conn, monkeypatch):
    """`redact_privacy_batch` answers a record the filter raised on with an error AND the record's raw text. Written
    as the disclosure, that raw text would be what every grantee read serves, current for good by its hash."""
    from topos.disclosure.tier import apply_disclosure_tier_to_rows
    from topos.sanitization import privacy_filter

    def broken(text, transform_id, params=None):
        raise RuntimeError("model failed")

    monkeypatch.setattr(privacy_filter, "privacy_filter_available", lambda: True)
    monkeypatch.setattr(privacy_filter, "apply_text_transform_with_privacy_filter", broken)
    answer = privacy_filter.redact_privacy_batch(
        [{"id": "conversation_messages:m1:content", "text": "Email alice@example.com"}])
    assert answer["status"] == "ok" and answer["items"][0]["error"]
    assert answer["items"][0]["text"] == "Email alice@example.com"     # the raw text rides along
    client = AsyncMock()
    client.redact_batch = AsyncMock(return_value=answer)
    batch = [{"message_id": "m1", "content": "Email alice@example.com", "_table": "conversation_messages"}]
    result = await run_privacy_disclosure_layer(memory_conn, batch, client=client)
    assert result["records_updated"] == 0 and result["failed_records"] == 1
    stored = memory_conn.execute(
        "SELECT content, content_disclosure, content_disclosure_hash FROM conversation_messages WHERE message_id='m1'"
    ).fetchone()
    assert stored[1:] == (None, None)
    read = apply_disclosure_tier_to_rows([{"message_id": "m1", "content": stored[0], "content_disclosure": stored[1]}],
                                         table="conversation_messages", tier="default_disclosure")
    assert read[0]["content"] == "[disclosure pending]"
    # The next run with a working model fills the column: nothing marked the failed record done.
    client.redact_batch = AsyncMock(return_value={"items": [{"id": "conversation_messages:m1:content",
                                                             "text": "Email [EMAIL]"}],
                                                  "model": "openai/privacy-filter", "status": "ok"})
    batch = [{"message_id": "m1", "content": "Email alice@example.com", "_table": "conversation_messages"}]
    assert (await run_privacy_disclosure_layer(memory_conn, batch, client=client))["records_updated"] == 1
    assert memory_conn.execute("SELECT content_disclosure FROM conversation_messages").fetchone()[0] == "Email [EMAIL]"
