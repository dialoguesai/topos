"""Tests for GitHub activity ui_stream ingest → raw retention + activity_events."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
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
