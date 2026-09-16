"""Every row the demo/declared lanes emit names its canonical table.

Only a row whose table differed from the group's was stamped, and the schedule,
documents, financial and places groups have no default stamp, so the fact
extractor typed those rows by their keys. A declared calendar or document column
named like a journal or profile column (entry_at, mood_tag, record_type plus
organization) made the row an owner-written journal entry or profile record.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.facts.extract import extract_facts_from_batch
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.definitions import DataSourceDefinition
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

STATEMENT = "I work at Ferrograph Instruments"
RECORD = {"id": "declared-1", "text": STATEMENT, "when": "2026-09-01T10:00:00+00:00", "org": "Ferrograph Instruments"}
CALENDAR = ("schedule", "demo_calendar", "calendar_events", {"event_id": "id", "title": "text", "content": "text", "start_at": "when"})
DOCUMENTS = ("documents", "documents", "documents", {"doc_id": "id", "title": "text", "content": "text"})
SHAPES = {
    "calendar_entry_at": (CALENDAR, {"entry_at": "when"}),
    "calendar_mood_tag": (CALENDAR, {"mood_tag": {"const": "ok"}}),
    "calendar_record_type_organization": (CALENDAR, {"record_type": {"const": "job"}, "organization": "org"}),
    "documents_entry_at": (DOCUMENTS, {"entry_at": "when"}),
}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_all_migrations(connection)
    ConversationsTablesManager(connection).ensure_tables()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: connection)
    yield connection


@pytest.mark.parametrize(("lane", "declaration"), list(SHAPES.values()), ids=list(SHAPES))
def test_a_declared_column_cannot_retype_the_row(conn, lane, declaration):
    group, mapper_id, table, fields = lane
    source = DataSourceDefinition(
        source_id="declared_rows_probe", display_name="Declared rows probe", source_type="ui_stream",
        schema_id="declared.rows.v1", parser_id="declared.rows.v1", canonical_group_id=group,
        canonical_mapper_id=mapper_id, canonical_field_map={table: {**fields, **declaration}},
    )
    result = canonicalize_normalized_batch(
        conn, source, [NormalizedRecord(record_id=RECORD["id"], payload=dict(RECORD))],
        dataset_id="owner:default", sync_batch_id="batch-1",
    )
    assert not result.errors
    assert [(row["_table"], row["content"]) for row in result.canonical_records] == [(table, STATEMENT)]

    assert extract_facts_from_batch(conn, result.canonical_records) == 0
    assert [json.loads(raw) for (raw,) in conn.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact'")] == []
