from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from topos.features.timeline_projection import (
    project_canonical_timeline,
    project_timeline_rows,
)
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.local_sync import _run_local_sync_enrichment_if_enabled
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.registry import BROWSER_VISITS
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "timeline-projection.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    yield db
    db.close()


@pytest.mark.parametrize(
    ("table", "record_id_field", "record_id", "timestamp_field"),
    [
        ("activity_events", "event_id", "activity-1", "occurred_at"),
        ("journal_entries", "entry_id", "journal-1", "entry_at"),
        ("location_events", "event_id", "location-1", "occurred_at"),
        ("conversation_messages", "message_id", "conversation-1", "ts"),
        ("ai_chat_messages", "message_id", "ai-1", "ts"),
        ("calendar_events", "event_id", "calendar-1", "starts_at"),
        ("profile_records", "record_id", "profile-1", "created_at"),
        ("financial_transactions", "transaction_id", "financial-1", "posted_at"),
    ],
)
def test_projection_supports_every_canonical_family(
    conn,
    table,
    record_id_field,
    record_id,
    timestamp_field,
) -> None:
    row = {
        "_table": table,
        record_id_field: record_id,
        timestamp_field: "2026-07-13T12:00:00Z",
        "source_id": "test-source",
    }
    result = project_timeline_rows(conn, [row])

    assert result.written == 1
    stored = conn.execute(
        "SELECT canonical_table, source_id FROM timeline WHERE record_id=?",
        (record_id,),
    ).fetchone()
    assert tuple(stored) == (table, "test-source")


def test_projection_skips_excluded_and_invalid_rows(conn) -> None:
    conn.execute(
        """
        INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key)
        VALUES ('ex-1', 'record', 'excluded-1')
        """
    )
    result = project_timeline_rows(
        conn,
        [
            {"record_id": "excluded-1", "_table": "journal_entries", "entry_at": "2026-01-01"},
            {"record_id": "no-time", "_table": "journal_entries"},
            {"_table": "journal_entries", "entry_at": "2026-01-01"},
        ],
    )

    assert result.excluded == 1
    assert result.missing_timestamp == 1
    assert result.missing_record_id == 1
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 0


def test_projection_preserves_richer_existing_metadata(conn) -> None:
    row = {
        "record_id": "journal-1",
        "_table": "journal_entries",
        "entry_at": "2026-07-13T12:00:00Z",
        "source_id": "journal",
        "entity_ids": ["entity-1"],
        "signal_dimension": "beliefs",
    }
    project_timeline_rows(conn, [row])
    project_timeline_rows(
        conn,
        [
            {
                "record_id": "journal-1",
                "_table": "journal_entries",
                "entry_at": "2026-07-13T12:00:00Z",
                "source_id": "journal",
            }
        ],
    )

    stored = conn.execute(
        "SELECT entity_ids_json, signal_dimension FROM timeline WHERE record_id='journal-1'"
    ).fetchone()
    assert tuple(stored) == ('["entity-1"]', "beliefs")


def test_missing_only_classifies_stale_timestamp_without_creating_duplicate(conn) -> None:
    original = {
        "entry_id": "journal-stale",
        "_table": "journal_entries",
        "entry_at": "2026-07-13T12:00:00Z",
        "source_id": "journal",
        "entity_ids": ["entity-1"],
    }
    project_timeline_rows(conn, [original])
    corrected = {**original, "entry_at": "2026-07-13T13:00:00Z", "entity_ids": []}

    audit = project_timeline_rows(conn, [corrected], missing_only=True, dry_run=True)
    assert audit.existing == 1
    assert audit.timestamp_mismatch == 1
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 1

    migrated = project_timeline_rows(conn, [corrected])
    assert migrated.written == 1
    stored = conn.execute(
        "SELECT event_at, entity_ids_json FROM timeline WHERE record_id='journal-stale'"
    ).fetchone()
    assert tuple(stored) == ("2026-07-13T13:00:00+00:00", '["entity-1"]')
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 1


def test_missing_only_repairs_conflicting_projection_identity(conn) -> None:
    conn.execute(
        """
        INSERT INTO timeline (
            event_at, record_id, source_id, canonical_table, entity_ids_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "2026-07-13T13:00:00+00:00",
            "journal-1-loc",
            "journal",
            "journal_entries",
            '["place-1"]',
        ),
    )
    location_row = {
        "event_id": "journal-1-loc",
        "_table": "location_events",
        "event_at": "2026-07-13T13:00:00Z",
        "source_id": "journal",
    }

    audit = project_timeline_rows(conn, [location_row], missing_only=True, dry_run=True)
    assert audit.identity_mismatch == 1
    repaired = project_timeline_rows(conn, [location_row], missing_only=True)
    assert repaired.written == 1
    stored = conn.execute(
        "SELECT canonical_table, entity_ids_json FROM timeline WHERE record_id='journal-1-loc'"
    ).fetchone()
    assert tuple(stored) == ("location_events", '["place-1"]')
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 1


def test_repair_dry_run_filters_and_is_idempotent(conn) -> None:
    for index, timestamp in enumerate(("2026-07-12T12:00:00Z", "2026-07-13T12:00:00Z")):
        canonicalize_normalized_batch(
            conn,
            BROWSER_VISITS,
            [
                NormalizedRecord(
                    record_id=f"visit-{index}",
                    payload={
                        "record_id": f"visit-{index}",
                        "url": f"https://example.com/{index}",
                        "visited_at": timestamp,
                    },
                )
            ],
            dataset_id="owner:default",
            sync_batch_id=f"batch-{index}",
        )
    conn.execute("DELETE FROM timeline")
    conn.execute(
        """
        INSERT INTO timeline (event_at, record_id, source_id, canonical_table)
        VALUES ('2026-07-13T15:00:00+00:00', 'orphan-1', 'browser_visits', 'activity_events')
        """
    )
    conn.commit()

    report = project_canonical_timeline(
        conn,
        source_id="browser_visits",
        date_from=datetime(2026, 7, 13, tzinfo=timezone.utc),
        missing_only=True,
        dry_run=True,
    )
    assert report["totals"]["written"] == 1
    assert report["orphaned"]["total"] == 1
    assert report["orphaned"]["samples"][0]["record_id"] == "orphan-1"
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 1

    applied = project_canonical_timeline(
        conn,
        source_id="browser_visits",
        missing_only=True,
    )
    assert applied["totals"]["written"] == 2
    repeated = project_canonical_timeline(
        conn,
        source_id="browser_visits",
        missing_only=True,
    )
    assert repeated["totals"]["written"] == 0
    assert repeated["totals"]["existing"] == 2


def test_local_sync_projects_timeline_even_without_enrichment_source(conn) -> None:
    _run_local_sync_enrichment_if_enabled(
        db_conn=conn,
        source_id="source-without-enrichment-registration",
        canonical_messages=[
            {
                "message_id": "local-message-1",
                "ts": "2026-07-13T14:00:00Z",
                "source_id": "imessage",
            }
        ],
    )

    stored = conn.execute(
        "SELECT canonical_table, source_id FROM timeline WHERE record_id='local-message-1'"
    ).fetchone()
    assert tuple(stored) == ("conversation_messages", "imessage")


def test_coverage_api_detects_deleted_timeline_row(conn, monkeypatch) -> None:
    from topos.api.enrichment import _enrichment_coverage_core

    monkeypatch.setattr("topos.api.enrichment.get_db_connection", lambda: conn)

    conn.execute(
        """
        INSERT INTO activity_events (event_id, source_id, occurred_at, url, title)
        VALUES ('cov-event-1', 'browser_visits', '2026-07-13T12:00:00Z', 'https://example.com', 'Example')
        """
    )
    project_canonical_timeline(conn, source_id="browser_visits", missing_only=True)
    conn.execute("DELETE FROM timeline WHERE record_id='cov-event-1'")

    coverage = _enrichment_coverage_core("browser_visits")
    timeline_job = next(job for job in coverage["jobs"] if job["job_id"] == "timeline")
    assert timeline_job["enriched_records"] == 0
    assert timeline_job["coverage_percent"] == 0.0


def test_only_missing_backfill_restores_timeline(conn, monkeypatch) -> None:
    import asyncio

    from topos.api.enrichment import _generic_backfill_core
    from topos.sources.registry import REGISTRY

    monkeypatch.setattr("topos.api.enrichment.get_db_connection", lambda: conn)

    conn.execute(
        """
        INSERT INTO activity_events (event_id, source_id, occurred_at, url, title)
        VALUES ('backfill-event-1', 'browser_visits', '2026-07-13T12:00:00Z', 'https://example.com', 'Example')
        """
    )
    project_canonical_timeline(conn, source_id="browser_visits", missing_only=True)
    conn.execute("DELETE FROM timeline WHERE record_id='backfill-event-1'")

    source_def = REGISTRY.get("browser_visits")
    result = asyncio.run(
        _generic_backfill_core(source_def=source_def, job_name="timeline", only_missing=True)
    )
    assert result["rows_processed"] >= 1
    assert conn.execute(
        "SELECT COUNT(*) FROM timeline WHERE record_id='backfill-event-1'"
    ).fetchone()[0] == 1

    repeated = asyncio.run(
        _generic_backfill_core(source_def=source_def, job_name="timeline", only_missing=True)
    )
    assert repeated["rows_processed"] == 0

