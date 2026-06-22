"""Tests for extraction artifacts and structured signal path."""

from __future__ import annotations

import sqlite3

from topos.features.signal.extraction.artifact_router import route_canonical_record
from topos.features.signal.extraction.artifact_store import ExtractionArtifactStore
from topos.features.signal.signal_object_store import SignalObjectStore
from topos.features.signal.structured_signal import (
    is_narrative_primary_dimension,
    should_update_narrative_brief,
)
from topos.storage.db.migrations.extraction_artifacts import apply_extraction_artifacts_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_signal_objects_up(conn)
    apply_extraction_artifacts_up(conn)
    return conn


def test_calendar_produces_intervals() -> None:
    conn = _conn()
    record = {
        "event_id": "cal-006",
        "starts_at": "2026-03-16T11:00:00Z",
        "ends_at": "2026-03-16T13:00:00Z",
        "is_busy": False,
        "location": "",
    }
    result = route_canonical_record(conn, canonical_table="calendar_events", record=record)
    assert result["artifacts"] >= 1
    assert result["objects"] >= 1
    artifacts = ExtractionArtifactStore(conn).list_artifacts(artifact_type="Interval")
    assert len(artifacts) == 1
    assert artifacts[0]["payload"]["availability_kind"] == "free"


def test_calendar_busy_from_metadata_json() -> None:
    conn = _conn()
    record = {
        "event_id": "cal-006",
        "starts_at": "2026-03-16T11:00:00Z",
        "ends_at": "2026-03-16T13:00:00Z",
        "metadata_json": {"is_busy": False},
    }
    route_canonical_record(conn, canonical_table="calendar_events", record=record)
    artifacts = ExtractionArtifactStore(conn).list_artifacts(artifact_type="Interval")
    assert artifacts[0]["payload"]["availability_kind"] == "free"


def test_harness_contacts_produce_entity_refs() -> None:
    conn = _conn()
    record = {
        "contact_id": "contact-sara",
        "display_name": "Sara Chen",
        "identifier_type": "email",
    }
    result = route_canonical_record(conn, canonical_table="contacts", record=record)
    assert result["artifacts"] == 1
    artifacts = ExtractionArtifactStore(conn).list_artifacts(artifact_type="EntityRef")
    assert artifacts[0]["payload"]["entity_key"] == "sara-chen"


def test_router_idempotent_on_reingest() -> None:
    conn = _conn()
    record = {
        "event_id": "cal-001",
        "starts_at": "2026-03-13T10:00:00Z",
        "ends_at": "2026-03-13T11:00:00Z",
        "is_busy": True,
    }
    route_canonical_record(conn, canonical_table="calendar_events", record=record)
    route_canonical_record(conn, canonical_table="calendar_events", record=record)
    artifacts = ExtractionArtifactStore(conn).list_artifacts(artifact_type="Interval")
    assert len(artifacts) == 1


def test_non_narrative_dimensions_skip_prose_merge_flag() -> None:
    assert is_narrative_primary_dimension("memory") is True
    assert is_narrative_primary_dimension("time") is False
    assert is_narrative_primary_dimension("relationships") is False


def test_narrative_brief_optional_enables_llm_merge() -> None:
    assert should_update_narrative_brief("memory") is True
    assert should_update_narrative_brief("time") is True
    assert should_update_narrative_brief("work") is True
    assert should_update_narrative_brief("relationships") is True
    assert should_update_narrative_brief("intentions") is True
    assert should_update_narrative_brief("profile") is True


def test_message_produces_relationship_edge_object() -> None:
    conn = _conn()
    record = {
        "message_id": "msg-001",
        "sender_name": "Marcus Webb",
        "is_from_self": False,
        "content": "Are you free Thursday afternoon for Austin office visit?",
    }
    route_canonical_record(conn, canonical_table="conversation_messages", record=record)
    edges = SignalObjectStore(conn).list_objects("relationships", object_type="RelationshipEdge")
    assert len(edges) == 1
    assert edges[0]["payload"]["target_entity_key"] == "marcus-webb"
