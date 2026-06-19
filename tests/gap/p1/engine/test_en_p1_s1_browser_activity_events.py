"""
Gap: Browser — flat-only → activity_events canonical rows
Sprint: EN-P1-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.canonicalization.mappers.browser_activity_mapper import BrowserActivityCanonicalMapper
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.registry import BROWSER_VISITS
from topos.storage.canonical.activity_tables import ActivityEventsManager
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def test_browser_visit_maps_to_activity_events() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)

    assert BROWSER_VISITS.canonical_mapper_id == "browser_activity"
    assert BROWSER_VISITS.canonical_group_id == "activity"

    normalized = NormalizedRecord(
        record_id="visit-42",
        payload={
            "id": "visit-42",
            "url": "https://example.com/docs",
            "title": "Example Docs",
            "visited_at": "2026-01-03T08:00:00Z",
            "event_type": "visit",
        },
    )
    mapped = BrowserActivityCanonicalMapper().map(normalized)
    manager = ActivityEventsManager(conn)
    result = manager.upsert_batch(
        [mapped.payload],
        source_id="browser_visits",
        sync_batch_id="browser-batch-1",
    )
    assert result["events_created"] == 1

    row = conn.execute(
        """
        SELECT event_id, activity_type, url, source_id, sync_batch_id
        FROM activity_events WHERE event_id=?
        """,
        (mapped.payload["event_id"],),
    ).fetchone()
    assert row is not None
    assert row[1] == "visit"
    assert row[2] == "https://example.com/docs"
    assert row[3] == "browser_visits"
    assert row[4] == "browser-batch-1"

    second = manager.upsert_batch(
        [mapped.payload],
        source_id="browser_visits",
        sync_batch_id="browser-batch-2",
    )
    assert second["events_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1
