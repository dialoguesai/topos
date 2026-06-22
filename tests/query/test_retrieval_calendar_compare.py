"""Calendar compare retrieval for schedule:read raw mode."""

import sqlite3

import pytest

from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.demo_file_parsers import DemoCalendarParser
from topos.ingestion.sources.base import RawRecord
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.sources.registry import DEMO_CALENDAR_FILE
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "calendar_compare.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    parser = DemoCalendarParser(dataset_id="user:default:device")
    rows = [
        {
            "event_id": "cal-001",
            "title": "Investor sync",
            "starts_at": "2026-03-13T10:00:00Z",
            "ends_at": "2026-03-13T11:00:00Z",
            "location": "Zoom",
            "attendees": "Marcus Webb",
            "is_busy": "true",
        },
        {
            "event_id": "cal-002",
            "title": "Team standup",
            "starts_at": "2026-03-13T09:00:00Z",
            "ends_at": "2026-03-13T09:30:00Z",
            "location": "Meet",
            "attendees": "Jordan Lee",
            "is_busy": "true",
        },
        {
            "event_id": "cal-003",
            "title": "Focus block",
            "starts_at": "2026-03-13T13:00:00Z",
            "ends_at": "2026-03-13T15:00:00Z",
            "location": "Home",
            "attendees": "Jordan Lee",
            "is_busy": "true",
        },
        {
            "event_id": "cal-006",
            "title": "Open window",
            "starts_at": "2026-03-16T11:00:00Z",
            "ends_at": "2026-03-16T13:00:00Z",
            "location": "",
            "attendees": "Jordan Lee",
            "is_busy": "false",
        },
    ]
    for row in rows:
        norm = parser.parse(RawRecord(record_id=row["event_id"], payload=row))
        canonicalize_normalized_batch(
            c,
            DEMO_CALENDAR_FILE,
            [norm],
            dataset_id="user:default:device",
            sync_batch_id=f"batch-{row['event_id']}",
        )
    c.commit()
    yield c
    c.close()


def test_compare_march_dates_returns_both_days(conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("schedule:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="raw",
            query_text="Compare March 13 vs March 16 calendar density.",
        )
    )
    rows = bundle.context_packet.get("rows") or []
    dates = {str(row.get("starts_at") or "")[:10] for row in rows}
    assert "2026-03-13" in dates
    assert "2026-03-16" in dates
    assert len(rows) >= 4


def test_multi_source_calendar_retrieval(conn) -> None:
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore

    store = SQLiteCanonicalStore(conn)
    store.upsert(
        "calendar_events",
        {
            "record_id": "stub-cal-1",
            "event_id": "stub-cal-1",
            "title": "Stub source meeting",
            "starts_at": "2026-03-17T14:00:00Z",
            "ends_at": "2026-03-17T15:00:00Z",
            "source_id": "calendar_stub",
        },
        sync_batch_id="batch-stub",
    )
    conn.commit()

    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("schedule:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="raw",
            query_text="",
            installed_source_ids=["demo_calendar_file", "calendar_stub"],
        )
    )
    rows = bundle.context_packet.get("rows") or []
    record_ids = {str(row.get("record_id") or row.get("event_id") or "") for row in rows}
    assert "stub-cal-1" in record_ids
    assert any(rid.startswith("cal-") for rid in record_ids)
    assert len(rows) >= 5
