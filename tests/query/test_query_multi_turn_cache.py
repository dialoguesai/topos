"""Multi-turn query session cache: memory_hit avoids retrieval / DB access."""

from __future__ import annotations

import sqlite3

import pytest

from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.demo_file_parsers import DemoCalendarParser
from topos.ingestion.sources.base import RawRecord
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.source_generation import bump_source_generation
from topos.sources.registry import DEMO_CALENDAR_FILE
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def seeded_conn(tmp_path):
    db_path = tmp_path / "multi_turn.db"
    conn = sqlite3.connect(str(db_path))
    apply_all_migrations(conn)
    parser = DemoCalendarParser(dataset_id="user:default:device")
    for event_id, title, starts in (
        ("cal-mt-1", "Investor sync", "2026-03-13T10:00:00Z"),
        ("cal-mt-2", "Team standup", "2026-03-13T09:00:00Z"),
    ):
        row = {
            "event_id": event_id,
            "title": title,
            "starts_at": starts,
            "ends_at": "2026-03-13T11:00:00Z",
            "location": "Zoom",
            "attendees": "Jordan Lee",
            "is_busy": "true",
        }
        norm = parser.parse(RawRecord(record_id=event_id, payload=row))
        canonicalize_normalized_batch(
            conn,
            DEMO_CALENDAR_FILE,
            [norm],
            dataset_id="user:default:device",
            sync_batch_id=f"batch-{event_id}",
        )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def orchestrator(seeded_conn):
    adapters = AdapterFactory.create("local_database", conn=seeded_conn)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    orch._retrieval.reset_retrieve_call_count()
    return orch


@pytest.mark.asyncio
async def test_repeat_query_uses_memory_hit_without_second_retrieval(orchestrator) -> None:
    scope_id = "schedule:read"
    access_mode = "raw"
    query_text = "What meetings does Jordan have on March 13 2026?"
    manifest = resolve_scope_manifest(scope_id)
    session_id = "qs-multi-turn-repeat"

    first = await orchestrator.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert first["turn_outcome"] == "live_query"
    assert first["audit"]["retrieval_skipped"] is False
    assert orchestrator._retrieval.retrieve_call_count == 1
    assert first["audit"]["stores_touched"]

    second = await orchestrator.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert second["turn_outcome"] == "memory_hit"
    assert second["audit"]["retrieval_skipped"] is True
    assert second["audit"]["stores_touched"] == []
    assert orchestrator._retrieval.retrieve_call_count == 1
    assert second["public_result"] == first["public_result"]


@pytest.mark.asyncio
async def test_different_intent_still_live_queries(orchestrator) -> None:
    scope_id = "schedule:read"
    access_mode = "raw"
    manifest = resolve_scope_manifest(scope_id)
    session_id = "qs-multi-turn-diff"

    await orchestrator.execute(
        query_text="What meetings on March 13?",
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    calls_after_first = orchestrator._retrieval.retrieve_call_count

    second = await orchestrator.execute(
        query_text="What meetings on March 16?",
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert second["turn_outcome"] == "live_query"
    assert orchestrator._retrieval.retrieve_call_count == calls_after_first + 1


@pytest.mark.asyncio
async def test_new_access_mode_requires_boundary_within_session(orchestrator) -> None:
    scope_id = "schedule:read"
    query_text = "Summarize Jordan calendar March 13"
    manifest = resolve_scope_manifest(scope_id)
    session_id = "qs-multi-turn-mode"

    await orchestrator.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode="raw",
        manifest=manifest,
        query_session_id=session_id,
    )

    summary = await orchestrator.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode="summary",
        manifest=manifest,
        query_session_id=session_id,
    )
    assert summary["turn_outcome"] == "expand_boundary"


@pytest.mark.asyncio
async def test_ingest_bump_invalidates_cache_for_same_query(seeded_conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=seeded_conn)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    scope_id = "schedule:read"
    access_mode = "raw"
    query_text = "What meetings on March 13?"
    manifest = resolve_scope_manifest(scope_id)
    session_id = "qs-multi-turn-bump"

    first = await orch.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert first["turn_outcome"] == "live_query"

    cached = await orch.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert cached["turn_outcome"] == "memory_hit"
    calls_before_bump = orch._retrieval.retrieve_call_count

    bump_source_generation(seeded_conn, "demo_calendar_file")
    seeded_conn.commit()

    after_bump = await orch.execute(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        manifest=manifest,
        query_session_id=session_id,
    )
    assert after_bump["turn_outcome"] == "live_query"
    assert orch._retrieval.retrieve_call_count == calls_before_bump + 1
