"""Tests for canonical-first ingestion and signal derivation."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from topos.ingestion.canonical_pipeline import (
    activity_payload_to_signal_record,
    canonicalize_normalized_batch,
)
from topos.ingestion.ingest_helpers import ingest_ui_payload
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.registry import BROWSER_VISITS, CHATGPT_FILE, CHATGPT_UI
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def migrated_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical_pipeline.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    yield conn
    conn.close()


def test_browser_visit_canonicalize_writes_activity_events(migrated_conn) -> None:
    normalized = NormalizedRecord(
        record_id="visit-1",
        payload={
            "id": "visit-1",
            "url": "https://example.com/docs",
            "title": "Example Docs",
            "visited_at": "2026-01-03T08:00:00Z",
            "event_type": "visit",
        },
    )
    result = canonicalize_normalized_batch(
        migrated_conn,
        BROWSER_VISITS,
        [normalized],
        dataset_id="user-1:default:device1",
        sync_batch_id="batch-1",
    )
    assert result.events_created == 1
    assert len(result.canonical_records) == 1
    signal_row = result.canonical_records[0]
    assert signal_row["url"] == "https://example.com/docs"
    assert signal_row["event_id"].startswith("browser:")

    row = migrated_conn.execute(
        "SELECT event_id, url, source_id FROM activity_events WHERE event_id=?",
        (signal_row["event_id"],),
    ).fetchone()
    assert row is not None
    assert row["source_id"] == "browser_visits"


def test_activity_payload_to_signal_record_shape() -> None:
    record = activity_payload_to_signal_record(
        {"event_id": "browser:abc", "url": "https://a.test", "title": "A"},
        source_id="browser_visits",
    )
    assert record["record_id"] == "browser:abc"
    assert record["source_id"] == "browser_visits"


@pytest.mark.asyncio
async def test_browser_direct_ingest_runs_signal_derive_from_canonical(migrated_conn) -> None:
    mock_engine_result = MagicMock()
    mock_engine_result.status = "completed"
    mock_engine_result.error = None
    mock_engine_result.output = {
        "category": "technology",
        "confidence": 0.91,
        "model": "test-model",
        "items": [{"category": "technology", "confidence": 0.91, "model": "test-model"}],
    }

    with patch(
        "topos.enrichment.jobs.canonical.url_classification_core.run_engine_task",
        new=AsyncMock(return_value=mock_engine_result),
    ):
        result = await ingest_ui_payload(
                dataset_id="user-1:default:device1",
                schema_id="browser.visits.v1",
                payload={
                    "url": "https://example.com/docs",
                    "visited_at": "2026-05-31T20:38:56.765Z",
                    "title": "Example Docs",
                },
            source_id="browser_visits",
        )

    assert result["status"] == "ok"
    assert migrated_conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1
    enrichment = migrated_conn.execute(
        "SELECT url_category, record_id FROM browser_url_classification WHERE enriched_from_table='activity_events'"
    ).fetchone()
    assert enrichment is not None
    assert enrichment["url_category"] == "technology"
    fact = migrated_conn.execute(
        """
        SELECT dimension, payload_json FROM signal_facts
        WHERE source_id='browser_visits' AND payload_json LIKE '%technology%'
        """
    ).fetchone()
    assert fact is not None
    assert fact["dimension"] == "interests"
    assert "technology" in (fact["payload_json"] or "")


@pytest.mark.asyncio
async def test_browser_deferred_ingest_projects_timeline_before_background_work(
    migrated_conn,
) -> None:
    result = await ingest_ui_payload(
        dataset_id="user-1:default:device1",
        schema_id="browser.visits.v1",
        payload={
            "url": "https://example.com/restart-safe",
            "visited_at": "2026-07-13T23:16:05.509Z",
            "title": "Restart Safe",
        },
        source_id="browser_visits",
        defer_enrichment=True,
    )

    assert result["status"] == "ok"
    assert result["enrichment_pending"] is True
    assert result["timeline_rows_written"] == 1
    canonical = migrated_conn.execute(
        "SELECT event_id, occurred_at FROM activity_events WHERE source_id='browser_visits'"
    ).fetchone()
    assert canonical is not None
    timeline = migrated_conn.execute(
        "SELECT event_at, canonical_table FROM timeline WHERE record_id=?",
        (canonical["event_id"],),
    ).fetchone()
    assert timeline is not None
    assert timeline["canonical_table"] == "activity_events"
    assert timeline["event_at"] == "2026-07-13T23:16:05.509000+00:00"


def test_conversations_canonicalize_stamps_table(migrated_conn) -> None:
    """Live-ingest conversation records must carry _table: without the stamp
    they are key-for-key identical to ai_chat records (sender_type='human',
    sender_id=None, ts/seq) and downstream classifiers cannot tell the
    families apart (PLAN_PROVENANCE_SPLIT P0.2)."""
    from topos.features.stats.engine import _table_for_row
    from topos.sources.registry import IMESSAGE

    normalized = NormalizedRecord(
        record_id="im-1",
        payload={
            "message_id": "im-1",
            "thread_id": "t1",
            "ts": "2026-06-01T10:00:00+00:00",
            "sender_type": "human",
            "sender_id": "+15551234567",
            "content": "hey there",
        },
    )
    result = canonicalize_normalized_batch(
        migrated_conn,
        IMESSAGE,
        [normalized],
        dataset_id="user-1:default:device1",
        sync_batch_id="batch-conv-1",
    )
    assert result.messages_created == 1
    assert len(result.canonical_records) == 1
    record = result.canonical_records[0]
    assert record["_table"] == "conversation_messages"
    assert _table_for_row(record) == "conversation_messages"


def test_ai_messages_canonicalize_stamps_table(migrated_conn) -> None:
    from topos.features.stats.engine import _table_for_row

    normalized = NormalizedRecord(
        record_id="cg-1",
        payload={
            "message_id": "cg-1",
            "thread_id": "conv-1",
            "ts": "2026-06-01T10:00:00+00:00",
            "sender_type": "human",
            "content": "I live in Austin now",
        },
    )
    result = canonicalize_normalized_batch(
        migrated_conn,
        CHATGPT_FILE,
        [normalized],
        dataset_id="user-1:default:device1",
        sync_batch_id="batch-ai-1",
    )
    assert result.messages_created == 1
    assert len(result.canonical_records) == 1
    record = result.canonical_records[0]
    assert record["_table"] == "ai_chat_messages"
    assert _table_for_row(record) == "ai_chat_messages"


def test_chatgpt_sources_declare_automatic_enrichment_for_ui_and_file() -> None:
    from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    assert CHATGPT_UI.enrichment_trigger == "automatic"
    assert CHATGPT_FILE.enrichment_trigger == "automatic"
    assert BROWSER_VISITS.raw_enrichment_jobs == []
    assert "url_classification" in BROWSER_VISITS.canonical_enrichment_jobs
    assert "embeddings" in BROWSER_VISITS.canonical_enrichment_jobs
    browser_jobs = resolved_signal_derivation_jobs(BROWSER_VISITS)
    assert "topic_clusters" in browser_jobs
    assert "topic_clusters" in resolved_signal_derivation_jobs(CHATGPT_FILE)
