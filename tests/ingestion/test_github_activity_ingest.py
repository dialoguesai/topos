"""Tests for GitHub activity ui_stream ingest → raw retention + activity_events
(+ dual-lane journal_entries rows for PushEvent commits)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.registry import GITHUB_ACTIVITY


@pytest.fixture
def migrated_conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "github_activity.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_github_activity_ui_stream_writes_activity_event(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    payload = {
        "id": "44851245900",
        "type": "PushEvent",
        "actor": {"id": 583231, "login": "jonny"},
        "repo": {"id": 41881900, "name": "dialogues/topos"},
        "payload": {"push_id": 21980276450, "size": 2, "ref": "refs/heads/main"},
        "public": True,
        "created_at": "2026-07-01T12:34:56Z",
    }

    result = await _ingest_ui_payload_direct(
        dataset_id="user:default:device",
        schema_id="github.activity.v1",
        payload=payload,
        job_id="job-github-1",
        source_id=GITHUB_ACTIVITY.source_id,
        defer_enrichment=True,
    )

    assert result["status"] == "ok"
    assert result["records_processed"] == 1
    assert result["canonical_events_created"] == 1

    row = migrated_conn.execute(
        """
        SELECT event_id, activity_type, url, title, occurred_at, source_id
        FROM activity_events
        WHERE event_id=?
        """,
        ("github:44851245900",),
    ).fetchone()
    assert row is not None
    assert row["activity_type"] == "push"
    assert row["url"] == "https://github.com/dialogues/topos"
    assert row["title"] == "dialogues/topos: pushed 2 commits"
    assert row["occurred_at"] == "2026-07-01T12:34:56Z"
    assert row["source_id"] == "github_activity"

    # Raw retention keeps the original event payload verbatim (repo/actor/payload).
    raw = migrated_conn.execute(
        "SELECT source_record_id, payload_json FROM raw_githubactivity_ui_stream"
    ).fetchone()
    assert raw is not None
    assert raw["source_record_id"] == "44851245900"
    raw_payload = json.loads(raw["payload_json"])
    assert raw_payload["type"] == "PushEvent"
    assert raw_payload["repo"]["name"] == "dialogues/topos"


@pytest.mark.asyncio
async def test_github_push_ui_stream_ingest_writes_journal_rows(migrated_conn, monkeypatch) -> None:
    """Full ui_stream path: PushEvent with commits → activity event + journal rows."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    result = await _ingest_ui_payload_direct(
        dataset_id="user:default:device",
        schema_id="github.activity.v1",
        payload=_push_event_with_commits(),
        job_id="job-github-dual-1",
        source_id=GITHUB_ACTIVITY.source_id,
        defer_enrichment=True,
    )
    assert result["status"] == "ok"
    assert result["canonical_events_created"] == 1
    assert result["canonical_messages_created"] == 2

    assert migrated_conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1
    rows = migrated_conn.execute(
        "SELECT entry_id, category, source_id FROM journal_entries ORDER BY entry_id"
    ).fetchall()
    assert [row["entry_id"] for row in rows] == [
        f"github:dialogues/topos:{'a' * 40}",
        f"github:dialogues/topos:{'b' * 40}",
    ]
    assert all(row["category"] == "code" for row in rows)
    assert all(row["source_id"] == "github_activity" for row in rows)


def _push_event_with_commits() -> dict:
    return {
        "id": "44851245900",
        "type": "PushEvent",
        "actor": {"id": 583231, "login": "jonny"},
        "repo": {"id": 41881900, "name": "dialogues/topos"},
        "payload": {
            "push_id": 21980276450,
            "size": 2,
            "ref": "refs/heads/main",
            "commits": [
                {
                    "sha": "a" * 40,
                    "message": "fix: tighten retry loop",
                    "timestamp": "2026-07-01T12:30:00Z",
                },
                {"sha": "b" * 40, "message": "docs: add sync notes"},
            ],
        },
        "public": True,
        "created_at": "2026-07-01T12:34:56Z",
    }


def test_push_event_canonicalize_writes_activity_and_journal_rows(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = _push_event_with_commits()
    result = canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-dual-1",
    )
    assert result.events_created == 1
    assert result.messages_created == 2  # journal lane
    assert not result.errors

    assert migrated_conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1

    rows = migrated_conn.execute(
        """
        SELECT entry_id, entry_at, starts_at, ends_at, mood_tag, category, content,
               duration, people, place_name, source_id, source_record_id,
               sync_batch_id, metadata_json
        FROM journal_entries ORDER BY entry_id
        """
    ).fetchall()
    assert len(rows) == 2
    first = rows[0]
    assert first["entry_id"] == f"github:dialogues/topos:{'a' * 40}"
    assert first["entry_at"] == "2026-07-01T12:30:00Z"  # commit timestamp
    assert first["starts_at"] is None
    assert first["ends_at"] == "2026-07-01T12:30:00Z"
    assert first["mood_tag"] is None
    assert first["category"] == "code"
    assert first["content"] == "fix: tighten retry loop"
    assert first["duration"] is None
    assert first["people"] is None
    assert first["place_name"] is None
    assert first["source_id"] == "github_activity"
    assert first["source_record_id"] == f"44851245900:{'a' * 40}"
    assert first["sync_batch_id"] == "batch-dual-1"
    metadata = json.loads(first["metadata_json"])
    assert metadata["repo"] == "dialogues/topos"
    assert metadata["sha"] == "a" * 40
    assert metadata["branch"] == "main"
    assert metadata["event_id"] == "github:44851245900"

    second = rows[1]
    assert second["entry_at"] == "2026-07-01T12:34:56Z"  # falls back to event created_at
    assert second["content"] == "docs: add sync notes"

    # Signal records carry per-record table stamps (mixed-family batch).
    tables = sorted(str(rec.get("_table")) for rec in result.canonical_records)
    assert tables == ["activity_events", "journal_entries", "journal_entries"]


def test_push_event_reingest_is_idempotent(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = _push_event_with_commits()
    normalized = [NormalizedRecord(record_id=payload["id"], payload=payload)]
    canonicalize_normalized_batch(
        migrated_conn, GITHUB_ACTIVITY, normalized,
        dataset_id="user:default:device", sync_batch_id="batch-dual-1",
    )
    second = canonicalize_normalized_batch(
        migrated_conn, GITHUB_ACTIVITY, normalized,
        dataset_id="user:default:device", sync_batch_id="batch-dual-2",
    )
    assert second.events_created == 0
    assert second.messages_created == 0
    assert migrated_conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1
    assert migrated_conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 2
    # Re-ingest refreshed the batch stamp without duplicating rows.
    stamps = [
        row[0]
        for row in migrated_conn.execute(
            "SELECT DISTINCT sync_batch_id FROM journal_entries"
        ).fetchall()
    ]
    assert stamps == ["batch-dual-2"]


def test_pull_request_event_writes_no_journal_rows(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = {
        "id": "44851245901",
        "type": "PullRequestEvent",
        "actor": {"id": 583231, "login": "jonny"},
        "repo": {"id": 41881900, "name": "dialogues/topos"},
        "payload": {
            "action": "opened",
            "number": 42,
            "pull_request": {"number": 42, "html_url": "https://github.com/dialogues/topos/pull/42"},
        },
        "public": True,
        "created_at": "2026-07-01T12:34:56Z",
    }
    result = canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-pr-1",
    )
    assert result.events_created == 1
    assert result.messages_created == 0
    assert migrated_conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_github_activity_rejects_record_missing_type(migrated_conn, monkeypatch) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    result = await _ingest_ui_payload_direct(
        dataset_id="user:default:device",
        schema_id="github.activity.v1",
        payload={"id": "44851245901", "created_at": "2026-07-01T12:34:56Z"},
        job_id="job-github-2",
        source_id=GITHUB_ACTIVITY.source_id,
        defer_enrichment=True,
    )

    assert result["status"] == "error"
    assert "type" in result["error"]
    assert migrated_conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 0
