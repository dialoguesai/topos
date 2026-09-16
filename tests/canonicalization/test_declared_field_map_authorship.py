"""A declared field map cannot say the owner wrote a row.

Values a declaration resolves are text, so from_self "0" and is_from_self False
("false") were truthy to the legacy writer, and a declared sender_id "Self" read as
the owner. Each became an owner-authored conversation_messages row that the rules
extractor turned into an owner fact. The same keys on a row aimed at any other
table did it too: a row with no table stamp is typed by its keys (sender_id is a
message, sender_type 'user' is the owner's AI chat turn) or by its own declared
_table. Who wrote a row is the producer's fact: the mapper drops declared
authorship on every table, stamps minted messages as someone else's, and the
install refuses such a declaration.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from topos.canonicalization.declared_field_map import UNDECLARABLE_COLUMNS, DeclaredFieldMapper
from topos.canonicalization.mappers.base import CanonicalMapper, CanonicalRecord, MappingMetadata
from topos.features.facts.extract import extract_facts_from_batch
from topos.features.provenance.roles import owner_authored
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.base import NormalizedRecord
from topos.sources.definitions import DataSourceDefinition
from topos.sources.install_service import _validate_source_contract
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

STATEMENT = "I work at Ferrograph Instruments"
RECORD = {
    "id": "declared-1",
    "text": STATEMENT,
    "sent_at": "2026-09-01T10:00:00+00:00",
    "who": "Self",
    "flag": False,
}
BASE_FIELDS = {
    "message_id": "id",
    "conversation_id": {"const": "declared-thread"},
    "content": "text",
    "event_at": "sent_at",
}
AUTHORSHIP_DECLARATIONS = {
    "from_self_const_0": {"from_self": {"const": "0"}},
    "sender_id_path_Self": {"sender_id": "who"},
    "is_from_self_path_False": {"is_from_self": "flag"},
}
CALENDAR_FIELDS = {"event_id": "id", "title": "text", "content": "text", "start_at": "sent_at"}
DOCUMENT_FIELDS = {"doc_id": "id", "title": "text", "content": "text"}
AS_AI_CHAT = {"sender_type": {"const": "human"}}
OTHER_TABLE_DECLARATIONS = {
    "calendar_sender_id_path_Self": ("schedule", "demo_calendar", "calendar_events", {"sender_id": "who"}),
    "calendar_is_from_self_const_1": ("schedule", "demo_calendar", "calendar_events", {"is_from_self": {"const": "1"}}),
    "calendar__table_ai_chat": (
        "schedule", "demo_calendar", "calendar_events", {"_table": {"const": "ai_chat_messages"}, **AS_AI_CHAT},
    ),
    "calendar_canonical_table_ai_chat": (
        "schedule", "demo_calendar", "calendar_events", {"canonical_table": {"const": "ai_chat_messages"}, **AS_AI_CHAT},
    ),
    "documents_sender_type_user": ("documents", "documents", "documents", {"sender_type": {"const": "user"}}),
}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    connection = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_all_migrations(connection)
    ConversationsTablesManager(connection).ensure_tables()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: connection)
    yield connection


def _works_at(connection):
    return [
        payload
        for (raw,) in connection.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact'")
        if (payload := json.loads(raw)).get("predicate") == "works_at"
    ]


@pytest.mark.parametrize("declaration", list(AUTHORSHIP_DECLARATIONS.values()), ids=list(AUTHORSHIP_DECLARATIONS))
def test_declared_authorship_writes_no_owner_fact(conn, declaration):
    source = DataSourceDefinition(
        source_id="declared_messages_probe",
        display_name="Declared messages probe",
        source_type="ui_stream",
        schema_id="declared.messages.v1",
        parser_id="declared.messages.v1",
        canonical_group_id="activity",
        canonical_field_map={"conversation_messages": {**BASE_FIELDS, **declaration}},
    )
    result = canonicalize_normalized_batch(
        conn, source, [NormalizedRecord(record_id=RECORD["id"], payload=dict(RECORD))],
        dataset_id="owner:default", sync_batch_id="batch-1",
    )
    assert not result.errors
    live = [row for row in result.canonical_records if row.get("_table") == "conversation_messages"]
    assert [row["content"] for row in live] == [STATEMENT]

    conn.row_factory = sqlite3.Row
    stored = [{**dict(row), "_table": "conversation_messages"} for row in conn.execute("SELECT * FROM conversation_messages")]
    conn.row_factory = None
    assert [row["content"] for row in stored] == [STATEMENT]

    assert extract_facts_from_batch(conn, live) == 0
    assert extract_facts_from_batch(conn, stored) == 0
    assert _works_at(conn) == []
    assert stored[0]["is_from_self"] == 0
    assert str(stored[0]["sender_id"] or "").casefold() != "self"


@pytest.mark.parametrize(
    ("group", "mapper_id", "table", "declaration"),
    list(OTHER_TABLE_DECLARATIONS.values()),
    ids=list(OTHER_TABLE_DECLARATIONS),
)
def test_authorship_declared_on_another_table_writes_no_owner_fact(conn, group, mapper_id, table, declaration):
    fields = CALENDAR_FIELDS if table == "calendar_events" else DOCUMENT_FIELDS
    source = DataSourceDefinition(
        source_id="declared_rows_probe",
        display_name="Declared rows probe",
        source_type="ui_stream",
        schema_id="declared.rows.v1",
        parser_id="declared.rows.v1",
        canonical_group_id=group,
        canonical_mapper_id=mapper_id,
        canonical_field_map={table: {**fields, **declaration}},
    )
    result = canonicalize_normalized_batch(
        conn, source, [NormalizedRecord(record_id=RECORD["id"], payload=dict(RECORD))],
        dataset_id="owner:default", sync_batch_id="batch-1",
    )
    assert not result.errors
    assert [row["content"] for row in result.canonical_records] == [STATEMENT]

    assert extract_facts_from_batch(conn, result.canonical_records) == 0
    assert _works_at(conn) == []


def test_every_table_drops_undeclarable_columns_with_a_receipt(caplog):
    mapper = DeclaredFieldMapper(
        source_id="declared_rows_probe",
        field_map={"calendar_events": {
            **CALENDAR_FIELDS, "sender_id": "who", **{column: {"const": "1"} for column in UNDECLARABLE_COLUMNS},
        }},
        default_table="calendar_events",
    )
    with caplog.at_level(logging.WARNING, logger="topos.canonicalization.declared_field_map"):
        (row,) = mapper.map_many(NormalizedRecord(record_id="declared-1", payload=dict(RECORD)))

    assert row.table is None
    assert not UNDECLARABLE_COLUMNS & set(row.payload)  # only a minted message is stamped someone else's
    assert row.payload["sender_id"] == "declared:Self"
    receipts = [record.getMessage() for record in caplog.records]
    for column in UNDECLARABLE_COLUMNS:
        assert any(f"dropped declared calendar_events.{column}:" in text for text in receipts)
    assert any("rewrote declared calendar_events.sender_id" in text for text in receipts)


@pytest.mark.parametrize("flag", ["1", 1.0])
def test_an_untyped_flag_is_not_the_owner_to_the_role_gate(flag):
    row = {"message_id": "declared-1", "content": STATEMENT, "is_from_self": flag}
    assert owner_authored(row, table="conversation_messages") is False
    assert owner_authored(row) is False


def test_minted_rows_drop_authorship_and_are_stamped_someone_elses(caplog):
    mapper = DeclaredFieldMapper(
        source_id="declared_messages_probe",
        field_map={"conversation_messages": {
            **BASE_FIELDS, "sender_id": "who", "is_from_self": {"const": "1"}, "from_self": {"const": "1"},
            "role": {"const": "user"}, "actor_role": {"const": "authored"}, "owner_user_id": {"const": "owner-1"},
        }},
    )
    with caplog.at_level(logging.WARNING, logger="topos.canonicalization.declared_field_map"):
        (row,) = mapper.map_many(NormalizedRecord(record_id="declared-1", payload=dict(RECORD)))

    assert row.table == "conversation_messages"
    assert {key: row.payload.get(key) for key in ("is_from_self", "actor_role", "sender_id")} == {
        "is_from_self": 0, "actor_role": "observed", "sender_id": "declared:Self",
    }
    assert not {"from_self", "role", "owner_user_id"} & set(row.payload)
    receipts = [record.getMessage() for record in caplog.records]
    for column in ("actor_role", "from_self", "is_from_self", "owner_user_id", "role"):
        assert any(f"dropped declared conversation_messages.{column}" in text for text in receipts)
    assert any("rewrote declared conversation_messages.sender_id" in text for text in receipts)


class _MessageMapper(CanonicalMapper):
    def map(self, normalized):
        return CanonicalRecord(record_id=normalized.record_id, table="conversation_messages", payload={
            "message_id": normalized.record_id, "content": "base", "sender_id": "+15555550142", "is_from_self": 0,
        })

    def mapping_metadata(self, normalized):
        return MappingMetadata(source_id="probe", mapping_version="v1")


def test_an_overlay_keeps_the_code_mappers_authorship():
    mapper = DeclaredFieldMapper(
        source_id="declared_messages_probe",
        field_map={"conversation_messages": {
            "content": "text", "sender_id": "who", "is_from_self": {"const": "1"}, "owner_user_id": {"const": "owner-1"},
        }},
        base=_MessageMapper(),
    )
    (row,) = mapper.map_many(NormalizedRecord(record_id="declared-1", payload=dict(RECORD)))

    assert row.payload["content"] == STATEMENT  # the overlay still applies
    assert row.payload["is_from_self"] == 0
    assert row.payload["sender_id"] == "declared:Self"
    assert "owner_user_id" not in row.payload


def test_ai_chat_messages_is_not_a_declared_target(caplog):
    mapper = DeclaredFieldMapper(
        source_id="declared_chat_probe",
        field_map={"ai_chat_messages": {"message_id": "id", "content": "text", "sender_type": {"const": "human"}}},
    )
    with caplog.at_level(logging.WARNING, logger="topos.canonicalization.declared_field_map"):
        assert mapper.map_many(NormalizedRecord(record_id="declared-1", payload=dict(RECORD))) == []
    assert any("refused declared rows for ai_chat_messages" in record.getMessage() for record in caplog.records)


def _install_definition(field_map):
    return {
        "source_id": "declared_messages_probe",
        "display_name": "Declared messages probe",
        "source_type": "ui_stream",
        "schema_id": "declared.messages.v1",
        "parser_id": "declared.messages.v1",
        "canonical_mapper_id": "browser_activity",
        "canonical_group_id": "activity",
        "canonical_field_map": field_map,
    }


@pytest.mark.parametrize("column", ["is_from_self", "from_self", "role", "actor_role", "owner_user_id"])
@pytest.mark.parametrize("fan_out", [False, True])
def test_install_refuses_a_declared_authorship_column(column, fan_out):
    fields = {**BASE_FIELDS, column: {"const": "0"}}
    block = ({"fan_out": "items[*]", "fields": {**fields, "message_id": {"path": "id", "scope": "item"},
                                                "source_record_id": "id"}} if fan_out else fields)
    with pytest.raises(ValueError, match=f"conversation_messages'\\]\\.{column} cannot be declared"):
        _validate_source_contract(_install_definition({"conversation_messages": block}))


@pytest.mark.parametrize("column", sorted(UNDECLARABLE_COLUMNS))
@pytest.mark.parametrize(("table", "fields"), [("calendar_events", CALENDAR_FIELDS), ("conversation_messages", BASE_FIELDS)])
def test_install_refuses_undeclarable_columns_on_every_table(table, fields, column):
    field_map = {"activity_events": {"content": "text"}, table: {**fields, column: {"const": "human"}}}
    with pytest.raises(ValueError, match=f"\\[{table!r}\\]\\.{column} cannot be declared"):
        _validate_source_contract(_install_definition(field_map))


def test_install_refuses_ai_chat_messages_as_a_target():
    with pytest.raises(ValueError, match="ai_chat_messages"):
        _validate_source_contract(_install_definition({"ai_chat_messages": {"message_id": "id", "content": "text"}}))


def test_install_accepts_a_message_declaration_without_authorship():
    definition = _validate_source_contract(_install_definition({"conversation_messages": dict(BASE_FIELDS)}))
    assert definition["canonical_field_map"]["conversation_messages"] == BASE_FIELDS
