"""Fingerprint invalidates memory_hit after ingest bumps source generation."""

import sqlite3

import pytest

from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.demo_file_parsers import DemoCalendarParser
from topos.ingestion.sources.base import RawRecord
from topos.query.fingerprint import compute_retrieval_fingerprint
from topos.query.intent import compute_intent_hash
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.session import QueryArtifact, QuerySession
from topos.query.session_utils import build_cache_key
from topos.query.source_generation import bump_source_generation, get_data_health_version
from topos.query.turn_classifier import TurnClassifierLite
from topos.query.types import QueryTurn
from topos.sources.registry import DEMO_CALENDAR_FILE
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "fingerprint.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    yield c
    c.close()


def test_generation_bump_changes_fingerprint(conn) -> None:
    manifest = resolve_scope_manifest("schedule:read")
    source_ids = list(manifest.default_source_ids or ["demo_calendar_file"])
    before = get_data_health_version("schedule:read", source_ids, conn)
    bump_source_generation(conn, "demo_calendar_file")
    after = get_data_health_version("schedule:read", source_ids, conn)
    assert before != after

    fp_before = compute_retrieval_fingerprint(
        scope_id="schedule:read",
        access_mode="raw",
        source_ids=source_ids,
        data_health_version=before,
    )
    fp_after = compute_retrieval_fingerprint(
        scope_id="schedule:read",
        access_mode="raw",
        source_ids=source_ids,
        data_health_version=after,
    )
    assert fp_before != fp_after


def test_stale_artifact_triggers_live_query_after_ingest(conn) -> None:
    scope_id = "schedule:read"
    access_mode = "raw"
    query_text = "Compare March 13 vs March 16 calendar density."
    manifest = resolve_scope_manifest(scope_id)
    source_ids = list(manifest.default_source_ids or ["demo_calendar_file"])
    intent_hash = compute_intent_hash(scope_id=scope_id, access_mode=access_mode, query_text=query_text)

    stale_version = get_data_health_version(scope_id, source_ids, conn)
    stale_fp = compute_retrieval_fingerprint(
        scope_id=scope_id,
        access_mode=access_mode,
        source_ids=source_ids,
        data_health_version=stale_version,
    )
    session = QuerySession(
        session_id="qs_test",
        requester_id="owner",
        intent_hash=intent_hash,
        envelope_json={"scopes": [scope_id], "access_modes": [access_mode]},
        artifacts=[
            QueryArtifact(
                artifact_id="art1",
                session_id="qs_test",
                cache_key=build_cache_key(
                    scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash
                ),
                public_result_json={"rows": []},
                retrieval_fingerprint=stale_fp,
            )
        ],
    )
    turn = QueryTurn(
        query_text=query_text,
        scope_id=scope_id,
        access_mode=access_mode,
        intent_hash=intent_hash,
    )
    classifier = TurnClassifierLite()
    hit_before = classifier.classify(
        turn,
        session,
        source_ids=source_ids,
        data_health_version=stale_version,
    )
    assert hit_before.outcome.value == "memory_hit"

    parser = DemoCalendarParser(dataset_id="user:default:device")
    row = {
        "event_id": "cal-ingest-1",
        "title": "Investor sync",
        "starts_at": "2026-03-13T10:00:00Z",
        "ends_at": "2026-03-13T11:00:00Z",
        "location": "Zoom",
        "attendees": "Marcus Webb",
        "is_busy": "true",
    }
    norm = parser.parse(RawRecord(record_id=row["event_id"], payload=row))
    canonicalize_normalized_batch(
        conn,
        DEMO_CALENDAR_FILE,
        [norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-cal-ingest-1",
    )
    conn.commit()

    fresh_version = get_data_health_version(scope_id, source_ids, conn)
    live_after = classifier.classify(
        turn,
        session,
        source_ids=source_ids,
        data_health_version=fresh_version,
    )
    assert live_after.outcome.value == "live_query"
