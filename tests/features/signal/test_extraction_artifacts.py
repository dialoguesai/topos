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


# ---------------------------------------------- two people, one journal entry
#
# A journal entry declaring `people = "Rowan, Nadia"` emits one Edge per person,
# and every one of them carries the same `source_refs`. Two collapses used to eat
# that, one at each store, and both are pinned below.
#
# Measured on the live node 2026-08-28: the row naming two people held exactly
# ONE Edge artifact, and 22 journal rows declare two or more. One person's edges
# then collapsed to a single object whose surviving text was decided by write
# order — 16 artifacts said one project, the surviving row said the other.

JOURNAL_TWO_PEOPLE = {
    "entry_id": "tl-50",
    "entry_at": "2026-05-14T19:00:00",
    "category": "Chill",
    "people": "Rowan, Nadia",
    "content": "Walked down to the pizza place.",
    "source_id": "grow_journal",
}


def _edges(conn):
    return ExtractionArtifactStore(conn).list_artifacts(artifact_type="Edge")


def test_a_second_person_on_one_record_does_not_overwrite_the_first() -> None:
    conn = _conn()
    route_canonical_record(
        conn, canonical_table="journal_entries", record=JOURNAL_TWO_PEOPLE
    )

    targets = {a["payload"]["target_entity_key"] for a in _edges(conn)}
    assert targets == {"rowan", "nadia"}, (
        "both declared participants must survive; the artifact key has to carry "
        "who the edge is about, not only which record it came from"
    )


def test_re_ingesting_the_same_record_is_still_idempotent() -> None:
    """The identity field must discriminate WITHOUT reintroducing duplicates."""
    conn = _conn()
    for _ in range(3):
        route_canonical_record(
            conn, canonical_table="journal_entries", record=JOURNAL_TWO_PEOPLE
        )

    assert len(_edges(conn)) == 2


def test_one_persons_two_activities_are_two_objects() -> None:
    """A Topos session and a Chill evening are two facts about the same person.

    On a person-only key the second overwrites the first, and which one survives
    is decided by write order rather than by evidence.
    """
    conn = _conn()
    route_canonical_record(
        conn,
        canonical_table="journal_entries",
        record=dict(JOURNAL_TWO_PEOPLE, entry_id="tl-1", category="Topos", people="Rowan"),
    )
    route_canonical_record(
        conn,
        canonical_table="journal_entries",
        record=dict(JOURNAL_TWO_PEOPLE, entry_id="tl-2", category="Chill", people="Rowan"),
    )

    rows = SignalObjectStore(conn).list_objects(
        "relationships", object_type="RelationshipEdge", limit=50
    )
    bands = {
        r["payload"].get("coactivity_band")
        for r in rows
        if r["payload"].get("target_entity_key") == "rowan"
    }
    assert bands == {"Topos", "Chill"}


def test_the_edge_still_indexes_under_the_persons_name() -> None:
    """The store key is now `person|activity`; the INDEX must still name the
    person, not publish "Rowan|Topos" as though it were somebody."""
    from topos.features.signal.derived_index import render_relationship_edge

    class _R:
        identifier_names: dict = {}
        peer_message_counts: dict = {}

        def display_name_for_key(self, key):
            from topos.features.signal.derived_index import _NameResolver

            return _NameResolver.display_name_for_key(self, key)

    obj = {
        "object_id": "o1",
        "object_key": "rowan|Topos",
        "signal_dimension": "relationships",
        "payload": {
            "target_entity_key": "rowan",
            "tier": "personal",
            "coactivity_band": "Topos",
        },
    }
    rendering = render_relationship_edge(obj, _R())

    assert rendering is not None
    assert rendering.title == "Rowan"
    assert "|" not in rendering.title
    assert "shared activity: Topos" in rendering.text
