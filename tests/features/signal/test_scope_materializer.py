"""Tests for scope-named signal object materialization."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.signal_object_store import SignalObjectStore
from topos.features.signal.typed_stores.scope_materializer import materialize_scope_signal_objects
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def migrated_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "materializer.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


def test_materialize_event_counts_and_memory_entities(migrated_conn) -> None:
    conn = migrated_conn
    conn.execute(
        """
        INSERT INTO calendar_events (
            event_id, source_id, title, starts_at, ends_at, metadata_json
        ) VALUES (
            'cal-a', 'demo_calendar_file', 'Busy block',
            '2026-03-13T10:00:00Z', '2026-03-13T11:00:00Z', '{"is_busy": true}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO calendar_events (
            event_id, source_id, title, starts_at, ends_at, metadata_json
        ) VALUES (
            'cal-b', 'demo_calendar_file', 'Open',
            '2026-03-16T11:00:00Z', '2026-03-16T13:00:00Z', '{"is_busy": false}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO message_entities (
            entity_id, record_id, source_id, entity_text, payload_json
        ) VALUES ('ent-1', 'msg-1', 'demo_messenger_file', 'Marcus Webb', '{}')
        """
    )
    conn.commit()

    counts = materialize_scope_signal_objects(conn)
    assert counts["event_counts"] == 1
    assert counts["message_entities"] == 1

    store = SignalObjectStore(conn)
    event_counts = store.list_objects("time", object_type="event_counts", limit=10)
    assert event_counts
    assert event_counts[0]["payload"]["total_events"] == 2

    entities = store.list_objects("memory", object_type="message_entities", limit=10)
    assert len(entities) == 1
    assert entities[0]["payload"]["entity_text"] == "Marcus Webb"
