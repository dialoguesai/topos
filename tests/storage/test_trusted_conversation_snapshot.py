"""Real enrollment/claim tests for the isolated native canonical insert lane."""
from contextlib import closing, contextmanager
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceBinding
from topos.permissions_v2.ingest_provenance import IngestProvenanceService, OWNER_ATTESTATION, VerifiedIngestContext
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore, _insert_trusted_conversation_batch
from topos.storage.canonical.conversations_tables import ConversationsTablesManager, ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import with_db_write


@contextmanager
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        yield
    finally:
        reset_principal(token)


@pytest.fixture
def enrolled(tmp_path):
    path = tmp_path / "canonical.db"
    conn = sqlite3.connect(path)
    apply_all_migrations(conn)
    ensure_all_tables(conn)
    with with_db_write():
        conn.execute("CREATE TABLE IF NOT EXISTS engine_config(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-1')")
        conn.commit()
    # Migration scratch TEMP state must not enter the worker's bound connection.
    conn.close()
    conn = sqlite3.connect(path)
    legacy = SQLiteCanonicalStore(conn)  # Schema work is before any trusted batch.
    ensure_protection_clock(path, owner_id="owner-1")
    root = tmp_path / "permissions-v2" / "ingest-snapshots"
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    snapshot = root / "native-fixture.db"
    with closing(sqlite3.connect(snapshot)) as source:
        source.execute("CREATE TABLE native_fixture(value TEXT)")
        source.execute("INSERT INTO native_fixture VALUES('synthetic snapshot')")
        source.commit()
    snapshot.chmod(0o400)
    service = IngestProvenanceService(canonical_database=path, snapshot_root=root,
        binding=EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1", owner_id="owner-1"))
    with owner():
        descriptor = service.describe_snapshot(conn, snapshot_id="native-fixture")
        enrollment = service.enroll(conn, snapshot_id="native-fixture", dataset_id="dataset-1",
            snapshot_sha256=descriptor["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION)
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"])
    context = service.claim(conn, job["job_id"])
    yield SimpleNamespace(conn=conn, path=path, service=service, context=context, legacy=legacy,
        manager=ConversationsTablesManager(conn), enrollment=enrollment)
    conn.close()


def message(message_id="imessage:1", **updates):
    return {"message_id": message_id, "thread_id": "thread-1", "dataset_id": "dataset-1",
        "source_id": "imessage", "sender_type": "human", "sender_id": "self", "from_self": True,
        "ts": "2026-09-15T10:00:00+00:00", "content": "Synthetic native text.", **updates}


def write(fixture, records, **options):
    return fixture.manager.upsert_message_batch(records, "dataset-1", "imessage",
        trusted_context=fixture.context, **options)


def rows(conn, table):
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def parent(conn, *, source="imessage"):
    with with_db_write():
        conn.execute("INSERT INTO conversations (conversation_id,dataset_id,source_id,created_at,updated_at) VALUES('thread-1','dataset-1',?,'original-created','original-updated')", (source,))
        conn.commit()


def legacy_row(fixture, **updates):
    fixture.legacy.upsert("conversation_messages", {
        "message_id": "imessage:1", "conversation_id": "thread-1", "dataset_id": "dataset-1",
        "source_id": "imessage", "source_record_id": "imessage:1", "owner_user_id": None,
        "sender_type": "human", "sender_id": "native-other", "is_from_self": False,
        "event_at": "2026-09-14T00:00:00+00:00", "content": "Historical synthetic content.", **updates,
    })


def test_new_owner_and_other_rows_keep_distinct_native_authorship_and_finish_atomically(enrolled):
    fixture = enrolled
    records = [message(), message("imessage:2", from_self=False, sender_id="native-other")]
    with fixture.context.batch(fixture.conn):
        result = write(fixture, records, sync_batch_id="native-batch")
        assert result == {"messages_created": 2, "conversations_created": 1,
            "message_ids": ["imessage:1", "imessage:2"], "historical_skipped": 0}
        assert fixture.conn.in_transaction
        with closing(sqlite3.connect(fixture.path)) as observer:
            assert observer.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 0
        fixture.service.finish(fixture.conn, fixture.context, {"status": "ok", "messages_processed": 2,
            **{key: result[key] for key in ("messages_created", "conversations_created", "historical_skipped")}})
    assert fixture.conn.execute("SELECT owner_user_id,is_from_self,sender_id,actor_role FROM conversation_messages ORDER BY message_id").fetchall() == [
        ("owner-1", 1, "self", "authored"), ("owner-1", 0, "native-other", "observed")]
    assert len(rows(fixture.conn, "ingest_provenance_records")) == 2
    for (metadata,) in fixture.conn.execute("SELECT metadata_json FROM conversation_messages"):
        assert json.loads(metadata) == {"topos_owner_ingest": {"version": "owner-attested-snapshot/v1",
            "enrollment_id": fixture.context.enrollment_id, "job_id": fixture.context.job_id}}
    assert rows(fixture.conn, "contacts") == []
    assert rows(fixture.conn, "conversation_participants") == []
    assert fixture.conn.execute("SELECT status FROM ingest_provenance_jobs").fetchone()[0] == "done"


def test_exception_after_insert_rolls_back_parent_message_and_provenance(enrolled):
    with pytest.raises(RuntimeError, match="abort synthetic transaction"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message()])
            raise RuntimeError("abort synthetic transaction")
    for table in ("conversations", "conversation_messages", "ingest_provenance_records", "contacts", "conversation_participants"):
        assert rows(enrolled.conn, table) == []


def test_provenance_insert_failure_rolls_back_every_row_and_parent(enrolled, monkeypatch):
    original = VerifiedIngestContext.record_insert
    def interrupted(context, conn, message_id):
        if message_id == "imessage:2":
            raise RuntimeError("synthetic provenance failure")
        return original(context, conn, message_id)
    monkeypatch.setattr(VerifiedIngestContext, "record_insert", interrupted)
    with pytest.raises(RuntimeError, match="synthetic provenance failure"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message(), message("imessage:2", thread_id="thread-2")])
    for table in ("conversations", "conversation_messages", "ingest_provenance_records"):
        assert rows(enrolled.conn, table) == []


@pytest.mark.parametrize("updates", [
    {"owner_user_id": "another-owner"}, {"source_id": "signal"}, {"dataset_id": "another-dataset"},
    {"source_record_id": "imessage:999"}, {"from_self": 1}, {"from_self": "false"},
    {"is_from_self": False}, {"sender_id": "native-other"}, {"sender_type": "assistant"},
    {"sender_type": []}, {"actor_role": "observed"}, {"role": "other"},
    {"from_self": False, "sender_id": "SELF"}, {"ts": "2026-09-15T10:00:00"},
    {"ts": "not-a-time"}, {"event_at": "2026-09-15T11:00:00+00:00"},
    {"content": None}, {"content": "invalid-\ud800"}, {"_metadata": {"invalid": float("nan")}},
    {"_metadata": {"topos_owner_ingest": None}},
    {"conversation_id": "different-thread"}, {"message_id": "import:1"}, {"trusted": True},
])
def test_invalid_later_record_is_rejected_before_any_canonical_write(enrolled, updates):
    statements = []
    with enrolled.context.batch(enrolled.conn):
        enrolled.conn.set_trace_callback(statements.append)
        try:
            with pytest.raises(PolicyError, match="ingest_canonical_invalid"):
                write(enrolled, [message(), {**message("imessage:2"), **updates}])
        finally:
            enrolled.conn.set_trace_callback(None)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)
    assert rows(enrolled.conn, "conversation_messages") == []
    assert rows(enrolled.conn, "conversations") == []


@pytest.mark.parametrize("owner_id", [None, "owner-1"])
def test_historical_rows_without_a_link_are_skipped_without_any_retroactive_stamp(enrolled, owner_id):
    parent(enrolled.conn)
    legacy_row(enrolled, owner_user_id=owner_id)
    before = {table: rows(enrolled.conn, table) for table in ("conversations", "conversation_messages")}
    with enrolled.context.batch(enrolled.conn):
        result = write(enrolled, [message(content="A new body must not heal this historical row.")])
    assert result == {"messages_created": 0, "conversations_created": 0, "message_ids": [], "historical_skipped": 1}
    assert {table: rows(enrolled.conn, table) for table in before} == before
    assert rows(enrolled.conn, "ingest_provenance_records") == []


@pytest.mark.parametrize("updates", [
    {"source_id": "another-source"}, {"dataset_id": "another-dataset"},
    {"conversation_id": "another-thread"}, {"source_record_id": "another-record"},
    {"owner_user_id": "another-owner"},
])
def test_existing_identity_collision_aborts_the_entire_batch_before_body_or_parent_changes(enrolled, updates):
    parent(enrolled.conn)
    legacy_row(enrolled, **updates)
    before = {table: rows(enrolled.conn, table) for table in ("conversations", "conversation_messages")}
    statements = []
    with enrolled.context.batch(enrolled.conn):
        enrolled.conn.set_trace_callback(statements.append)
        try:
            with pytest.raises(PolicyError, match="ingest_canonical_collision"):
                write(enrolled, [message("imessage:2", thread_id="new-thread"), message()])
        finally:
            enrolled.conn.set_trace_callback(None)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)
    assert {table: rows(enrolled.conn, table) for table in before} == before
    assert rows(enrolled.conn, "ingest_provenance_records") == []


def test_parent_source_collision_cannot_replace_parent_or_create_message(enrolled):
    parent(enrolled.conn, source="another-source")
    before = rows(enrolled.conn, "conversations")
    with pytest.raises(PolicyError, match="ingest_canonical_collision"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message()])
    assert rows(enrolled.conn, "conversations") == before
    assert rows(enrolled.conn, "conversation_messages") == []


def test_unknown_historical_source_record_identity_is_not_assumed_to_match(enrolled):
    legacy_row(enrolled)
    with with_db_write():
        enrolled.conn.execute("UPDATE conversation_messages SET source_record_id=NULL")
        enrolled.conn.commit()
    before = rows(enrolled.conn, "conversation_messages")
    with pytest.raises(PolicyError, match="ingest_canonical_collision"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message()])
    assert rows(enrolled.conn, "conversation_messages") == before
    assert rows(enrolled.conn, "conversations") == []


def test_historical_skip_does_not_recreate_a_missing_parent(enrolled):
    legacy_row(enrolled)
    with enrolled.context.batch(enrolled.conn):
        result = write(enrolled, [message()])
    assert result["historical_skipped"] == 1
    assert rows(enrolled.conn, "conversations") == []
    assert rows(enrolled.conn, "ingest_provenance_records") == []


def test_matching_parent_metadata_and_linked_replay_are_immutable(enrolled):
    parent(enrolled.conn)
    before_parent = rows(enrolled.conn, "conversations")
    with enrolled.context.batch(enrolled.conn):
        assert write(enrolled, [message(), message()])["messages_created"] == 1
        before = rows(enrolled.conn, "conversation_messages")
        links = rows(enrolled.conn, "ingest_provenance_records")
        replay = write(enrolled, [message()], sync_batch_id="another-batch")
        assert replay == {"messages_created": 0, "conversations_created": 0, "message_ids": ["imessage:1"], "historical_skipped": 0}
        assert rows(enrolled.conn, "conversation_messages") == before
        assert rows(enrolled.conn, "ingest_provenance_records") == links
    assert rows(enrolled.conn, "conversations") == before_parent


@pytest.mark.parametrize("updates", [
    {"content": "Altered snapshot content."}, {"ts": "2026-09-15T11:00:00+00:00"},
    {"from_self": False, "sender_id": "native-other"}, {"_metadata": {"owner_user_id": "owner-1"}},
])
def test_linked_replay_must_match_actual_canonical_content_time_and_authorship(enrolled, updates):
    with enrolled.context.batch(enrolled.conn):
        write(enrolled, [message()])
    before = rows(enrolled.conn, "conversation_messages")
    with pytest.raises(PolicyError, match="ingest_canonical_collision"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message("imessage:2"), message(**updates)])
    assert rows(enrolled.conn, "conversation_messages") == before
    assert len(rows(enrolled.conn, "ingest_provenance_records")) == 1


def test_conflicting_duplicate_ids_are_not_last_write_wins(enrolled):
    with pytest.raises(PolicyError, match="ingest_canonical_collision"):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message(), message(content="Conflicting body.")])
    assert rows(enrolled.conn, "conversation_messages") == []


def test_context_requires_actual_capability_and_its_active_connection_transaction(enrolled):
    with pytest.raises(PolicyError, match="ingest_transaction_required"):
        write(enrolled, [message()])
    with enrolled.context.batch(enrolled.conn):
        class OverriddenContext(VerifiedIngestContext):
            def require_batch(self, conn):
                pass
            def assert_current(self, conn, **kwargs):
                pass
        fake_service = SimpleNamespace(_require_batch=lambda *_: None, assert_current=lambda *_, **__: None)
        for fake in (SimpleNamespace(**vars(enrolled.context)), replace(enrolled.context),
                     OverriddenContext(**vars(enrolled.context)), replace(enrolled.context, service=fake_service)):
            with pytest.raises(PolicyError):
                _insert_trusted_conversation_batch(enrolled.conn, [message()], source_id="imessage",
                    dataset_id="dataset-1", trusted_context=fake)
        with closing(sqlite3.connect(enrolled.path)) as other:
            with pytest.raises(PolicyError, match="ingest_transaction_required"):
                _insert_trusted_conversation_batch(other, [message()], source_id="imessage",
                    dataset_id="dataset-1", trusted_context=enrolled.context)
    assert rows(enrolled.conn, "conversation_messages") == []


def test_legacy_store_has_no_trusted_keyword_that_can_bypass_the_private_insert_lane(enrolled):
    with pytest.raises(TypeError):
        enrolled.legacy.upsert("conversation_messages", message(), trusted_context=enrolled.context)
    assert rows(enrolled.conn, "conversation_messages") == []


def test_revoked_claim_cannot_insert_even_with_its_previous_real_context(enrolled):
    with owner():
        enrolled.service.revoke(enrolled.conn, enrollment_id=enrolled.enrollment["enrollment_id"])
    with pytest.raises(PolicyError):
        with enrolled.context.batch(enrolled.conn):
            write(enrolled, [message()])
    assert rows(enrolled.conn, "conversation_messages") == []
