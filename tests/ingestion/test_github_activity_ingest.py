"""Tests for GitHub activity ui_stream ingest → raw retention + activity_events
(+ dual-lane journal_entries rows for PushEvent commits)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.canonical_pipeline import (
    canonicalize_normalized_batch,
    load_canonical_records_for_signal,
)
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.registry import GITHUB_ACTIVITY


@pytest.fixture
def migrated_conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    # The ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    conn = sqlite3.connect(str(tmp_path / "github_activity.db"), check_same_thread=False)
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


def test_push_event_carries_commit_messages_into_activity_content(migrated_conn, monkeypatch) -> None:
    """§5a capability 2 (declared field map): the WORK a push describes is its
    commit messages. Before this, activity_events.content was NULL on every push
    row and the only text a semantic reader could see was the repo name."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = _push_event_with_commits()
    result = canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-content-1",
    )
    assert not result.errors

    row = migrated_conn.execute(
        "SELECT title, content FROM activity_events WHERE event_id=?",
        ("github:44851245900",),
    ).fetchone()
    # Title keeps counting the push; content carries what was actually done.
    assert row["title"] == "dialogues/topos: pushed 2 commits"
    assert row["content"] == "fix: tighten retry loop\n\ndocs: add sync notes"

    # The signal record handed to enrichment carries it too, so the row embeds
    # on its commit text rather than on its title.
    from topos.features.signal.embed_context import embeddable_content

    activity_records = [
        rec for rec in result.canonical_records if rec.get("_table") == "activity_events"
    ]
    assert len(activity_records) == 1
    assert embeddable_content(activity_records[0]) == "fix: tighten retry loop\n\ndocs: add sync notes"


def test_push_event_without_commits_leaves_content_null(migrated_conn, monkeypatch) -> None:
    """A commit-free push (Events-API shape with no commits[]) declares nothing;
    the column stays NULL rather than being filled with a placeholder."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = {
        "id": "44851245902",
        "type": "PushEvent",
        "actor": {"login": "jonny"},
        "repo": {"name": "dialogues/topos"},
        "payload": {"size": 2, "ref": "refs/heads/main"},
        "created_at": "2026-07-01T12:34:56Z",
    }
    canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-content-2",
    )
    row = migrated_conn.execute(
        "SELECT content FROM activity_events WHERE event_id=?", ("github:44851245902",)
    ).fetchone()
    assert row["content"] is None


def test_reingest_backfills_content_onto_a_content_free_row(migrated_conn, monkeypatch) -> None:
    """The backfill contract: rows written before the fix are healed by
    re-canonicalizing the retained raw record — no delete-and-reingest."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = _push_event_with_commits()
    migrated_conn.execute(
        """
        INSERT INTO activity_events (event_id, activity_type, title, occurred_at, source_id)
        VALUES (?, 'push', 'dialogues/topos: pushed 2 commits', '2026-07-01T12:34:56Z', 'github_activity')
        """,
        ("github:44851245900",),
    )
    migrated_conn.commit()

    canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-backfill-1",
    )
    row = migrated_conn.execute(
        "SELECT content FROM activity_events WHERE event_id=?", ("github:44851245900",)
    ).fetchone()
    assert row["content"] == "fix: tighten retry loop\n\ndocs: add sync notes"


def test_reloaded_activity_rows_keep_content_for_signal_derivation(migrated_conn, monkeypatch) -> None:
    """load_canonical_records_for_signal is the reprocess/backfill entry point:
    if it drops content, a backfill re-embeds titles and derives nothing new."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    payload = _push_event_with_commits()
    canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-reload-1",
    )
    reloaded = load_canonical_records_for_signal(migrated_conn, GITHUB_ACTIVITY)
    assert [rec.get("content") for rec in reloaded] == [
        "fix: tighten retry loop\n\ndocs: add sync notes"
    ]
    # metadata_json rides along so the declared ENTITY mapping (metadata_json.repo
    # → project + worked_on edge) resolves on reloaded rows too.
    assert json.loads(reloaded[0]["metadata_json"])["repo"] == "dialogues/topos"


def test_activity_signal_record_feeds_declared_entity_extraction(migrated_conn, monkeypatch) -> None:
    """§5a capability 4 reads `metadata_json.repo` off the record the entities
    job is handed. The activity signal record did not carry metadata_json, so
    on the live ingest path the declared mapping had nothing to resolve."""
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    from topos.features.entities.declared_mappings import extract_declared_entities

    payload = _push_event_with_commits()
    result = canonicalize_normalized_batch(
        migrated_conn,
        GITHUB_ACTIVITY,
        [NormalizedRecord(record_id=payload["id"], payload=payload)],
        dataset_id="user:default:device",
        sync_batch_id="batch-entities-1",
    )
    activity = [rec for rec in result.canonical_records if rec.get("_table") == "activity_events"][0]
    declared = extract_declared_entities(
        activity, record_id=activity["event_id"], event_at=activity.get("occurred_at")
    )
    assert [(row["entity_text"], row["entity_type"]) for row in declared] == [
        ("dialogues/topos", "project"),
        ("dialogues", "org"),
    ]
    assert declared[0]["self_edge"] == "worked_on"


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
