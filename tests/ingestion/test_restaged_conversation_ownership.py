"""Conversation rows re-staged from normalized records keep the parser's authorship and never a payload owner.

`canonical_pipeline.build_staging_record` copied neither `is_from_self` nor
`owner_user_id`, so a reprocess that inserted a raw row with no canonical row
yet stored the owner's own message as a correspondent's.

The flag is carried only when a bundled parser produced the record for a source
that apps cannot push into; a runtime parser's extract map can emit
`is_from_self` straight from raw JSON. `owner_user_id` is never carried: raw
payloads can hold a caller-supplied value (Signal upload), and reprocess is not
an attested owner lane. Every case runs against a scratch database and
synthetic rows.
"""
from __future__ import annotations

import dataclasses
import sqlite3
import types

import pytest

from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers import DemoMessengerParser, SignalParser
from topos.sources.definitions import DELIVERY_CLIENT_PUSH, DELIVERY_OWNER_UI
from topos.sources.registry import REGISTRY
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE = "demo_messenger_file"
DATASET = "ds-synthetic"


@pytest.fixture
def db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "scratch.db"), check_same_thread=False)
    apply_all_migrations(conn)
    ConversationsTablesManager(conn).ensure_tables()
    factory = lambda: conn  # noqa: E731
    import topos.core.state as state
    import topos.ingestion.reprocess as reprocess

    monkeypatch.setattr(state, "get_db_connection", factory)
    monkeypatch.setattr(reprocess, "get_db_connection", factory)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "off")
    yield conn
    conn.close()


def retain(conn, message_id, **fields):
    from topos.storage.raw.raw_tables_manager import RawTablesManager

    RawTablesManager(conn).write_raw_record(source_id=SOURCE, source_record_id=message_id, payload={
        "message_id": message_id, "conversation_id": "t1", "content": f"synthetic body {message_id}",
        "event_at": "2026-01-01T00:00:01Z", **fields})
    conn.commit()


def normalized(message_id, **fields):
    return {"message_id": message_id, "thread_id": "t1", "conversation_id": "t1", "ts": "2026-01-01T00:00:01Z",
            "sender_type": "human", "sender_id": "+15555550142", "content": f"synthetic body {message_id}", **fields}


def stored(conn):
    return {row[0]: tuple(row[1:]) for row in conn.execute(
        "SELECT message_id, is_from_self, owner_user_id FROM conversation_messages ORDER BY message_id")}


def canonicalize(conn, records, *, source_def=None, parser_cls=DemoMessengerParser):
    return canonicalize_normalized_batch(conn, source_def or REGISTRY[SOURCE], records, dataset_id=DATASET,
                                         sync_batch_id="batch-synthetic", parser_cls=parser_cls)


@pytest.mark.asyncio
async def test_reprocess_inserts_the_owners_retained_message_as_the_owners(db):
    from topos.ingestion.reprocess import reprocess_source

    retain(db, "m-own", is_from_self=True)
    retain(db, "m-other", is_from_self=False, sender_id="+15555550142")
    await reprocess_source(source_id=SOURCE, dataset_id=DATASET, run_enrichment=False)
    assert stored(db) == {"m-other": (0, None), "m-own": (1, None)}


@pytest.mark.asyncio
async def test_reprocess_does_not_promote_or_reown_a_row_that_already_exists(db):
    from topos.ingestion.reprocess import reprocess_source

    ConversationsTablesManager(db).upsert_message_batch(
        [{"message_id": "m-kept", "thread_id": "t1", "ts": "2026-01-01T00:00:00Z", "sender_type": "human",
          "content": "synthetic stored body"}], DATASET, SOURCE)
    retain(db, "m-kept", is_from_self=True)
    await reprocess_source(source_id=SOURCE, dataset_id=DATASET, run_enrichment=False)
    assert stored(db) == {"m-kept": (0, None)}


def test_an_owner_named_by_the_normalized_payload_is_never_stored(db):
    canonicalize(db, [normalized("m-claimed", is_from_self=True, owner_user_id="someone-else"),
                      normalized("m-alias", from_self=True, owner_user_id="someone-else")])
    assert stored(db) == {"m-alias": (1, None), "m-claimed": (1, None)}


@pytest.mark.parametrize("value", ["1", "true", "yes", 2, 1.0, None], ids=repr)
def test_only_a_typed_flag_is_carried(db, value):
    canonicalize(db, [normalized("m-text", is_from_self=value, from_self=value)])
    assert stored(db) == {"m-text": (0, None)}


def test_the_integer_one_is_carried_like_true(db):
    canonicalize(db, [normalized("m-int", is_from_self=1)])
    assert stored(db) == {"m-int": (1, None)}


def test_a_bundled_parser_class_under_another_bundled_id_still_counts(db):
    canonicalize(db, [normalized("m-signal", is_from_self=True)], parser_cls=SignalParser)
    assert stored(db) == {"m-signal": (1, None)}


def test_a_runtime_parsers_flag_is_not_carried(db):
    class RuntimeInstalledParser(DemoMessengerParser):
        """Shaped like runtime_install's dynamic class: not a bundled registration."""

    canonicalize(db, [normalized("m-runtime", is_from_self=True)], parser_cls=RuntimeInstalledParser)
    assert stored(db) == {"m-runtime": (0, None)}


def test_a_caller_that_names_no_parser_carries_no_flag(db):
    canonicalize(db, [normalized("m-unnamed", is_from_self=True)], parser_cls=None)
    assert stored(db) == {"m-unnamed": (0, None)}


@pytest.mark.parametrize("delivery", [DELIVERY_CLIENT_PUSH, DELIVERY_OWNER_UI, "stub"])
def test_a_source_the_node_did_not_read_or_the_owner_upload_carries_no_flag(db, delivery):
    if delivery == "stub":
        source_def = types.SimpleNamespace(source_id="synthetic_stub_messenger", canonical_group_id="conversations",
                                           canonical_mapper_id=None)
    else:
        source_def = dataclasses.replace(REGISTRY[SOURCE], source_id="synthetic_pushed_messenger", delivery=delivery)
    canonicalize(db, [normalized("m-pushed", is_from_self=True)], source_def=source_def)
    assert stored(db) == {"m-pushed": (0, None)}
