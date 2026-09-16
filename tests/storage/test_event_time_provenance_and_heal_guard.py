"""The legacy canonical message writer: what it may say about time, and what it may overwrite.

Two changes, each tested against the behaviour it replaces.

Time. The writer records ``topos-event-time/v1`` beside ``event_at`` on every new
row. It can vouch for exactly one thing: whether it, or the staging layer before
it, filled a missing native time with ingestion time. So it writes
``ingestion_clock_substitute`` or ``unverified_producer`` and never
``native_source_clock``, whatever a record claims.

Heal. A re-ingest may still correct a body the reader got wrong, but not across a
dataset or source (message IDs such as ``imessage:<ROWID>`` carry no dataset), and
not over a row with an owner-attested provenance link. It skips; it never raises.
"""
import json
import sqlite3

import pytest

from topos.features.temporal.records import EventTime
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.canonical.conversations_tables import ConversationsTablesManager, ensure_all_tables


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    ensure_all_tables(db)
    yield db
    db.close()


def message(**overrides):
    record = {"message_id": "imessage:7", "conversation_id": "chat-1", "dataset_id": "dataset-a",
              "source_id": "imessage", "sender_type": "human", "sender_id": "+15550000001",
              "content": "original body", "event_at": "2026-09-15T21:11:24+00:00"}
    record.update(overrides)
    return record


def stored(conn, message_id="imessage:7"):
    row = conn.execute("SELECT content, dataset_id, event_at, event_time_json FROM conversation_messages WHERE message_id=?",
                       (message_id,)).fetchone()
    return None if row is None else dict(zip(("content", "dataset_id", "event_at", "event_time_json"), row))


# --- time ---------------------------------------------------------------------

def test_a_supplied_time_is_recorded_as_unverified_never_as_native(conn):
    SQLiteCanonicalStore(conn).upsert("conversation_messages", message())
    row = stored(conn)
    record = EventTime.from_json(row["event_time_json"])
    assert record.event.text == row["event_at"] == "2026-09-15T21:11:24+00:00"
    assert record.event.provenance == "unverified_producer"


def test_a_missing_time_is_recorded_as_the_ingestion_clock_it_was_replaced_with(conn):
    SQLiteCanonicalStore(conn).upsert("conversation_messages", message(event_at=None))
    row = stored(conn)
    record = EventTime.from_json(row["event_time_json"])
    assert row["event_at"]  # the legacy column is still filled, exactly as before
    assert record.event.text == row["event_at"]
    assert record.event.provenance == "ingestion_clock_substitute"


def test_a_fill_made_upstream_is_marked_when_staging_says_so(conn):
    """local_sync fills a missing native time before the canonical writer sees it."""
    manager = ConversationsTablesManager(conn)
    manager.upsert_message_batch([{"message_id": "imessage:8", "thread_id": "chat-1", "sender_id": "self",
        "from_self": True, "content": "hello", "ts": "2026-09-16T10:00:00+00:00", "_event_time_substituted": True}],
        "dataset-a", "imessage")
    assert EventTime.from_json(stored(conn, "imessage:8")["event_time_json"]).event.provenance == "ingestion_clock_substitute"


@pytest.mark.parametrize("claim", ["native_source_clock", {"provenance": "native_source_clock"}])
def test_no_record_field_can_claim_a_native_clock(conn, claim):
    SQLiteCanonicalStore(conn).upsert("conversation_messages", message(
        event_time_json=json.dumps({"claim": claim}), provenance=claim, _event_time_substituted=False))
    assert EventTime.from_json(stored(conn)["event_time_json"]).event.provenance == "unverified_producer"


def test_a_database_without_the_column_is_written_exactly_as_before():
    db = sqlite3.connect(":memory:")
    ensure_all_tables(db)
    db.execute("ALTER TABLE conversation_messages DROP COLUMN event_time_json")
    SQLiteCanonicalStore(db).upsert("conversation_messages", message())
    assert db.execute("SELECT content FROM conversation_messages").fetchone() == ("original body",)


def test_the_event_time_column_is_added_without_rolling_back_pending_writes():
    """The sibling column helpers call rollback() on a duplicate column. This one must not."""
    db = sqlite3.connect(":memory:")
    ensure_all_tables(db)
    db.execute("BEGIN")
    db.execute("INSERT INTO conversations (conversation_id, dataset_id, source_id) VALUES ('pending', 'd', 's')")
    from topos.storage.canonical.conversations_tables import _ensure_event_time_column
    _ensure_event_time_column(db)
    assert db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id='pending'").fetchone() == (1,)


# --- heal ---------------------------------------------------------------------

def test_a_re_ingest_of_the_same_dataset_still_heals_a_wrong_body(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("conversation_messages", message(content="archive bytes"))
    store.upsert("conversation_messages", message(content="decoded body"))
    assert stored(conn)["content"] == "decoded body"


@pytest.mark.parametrize("change", [{"dataset_id": "dataset-b"}, {"source_id": "signal"}])
def test_a_same_rowid_from_another_dataset_or_source_never_rewrites_the_body(conn, change):
    store = SQLiteCanonicalStore(conn)
    store.upsert("conversation_messages", message())
    store.upsert("conversation_messages", message(content="another database's message", **change))
    row = stored(conn)
    assert row["content"] == "original body" and row["dataset_id"] == "dataset-a"


def test_a_row_with_an_attested_provenance_link_is_never_healed(conn):
    conn.execute("CREATE TABLE ingest_provenance_records (message_id TEXT PRIMARY KEY, enrollment_id TEXT, "
                 "enrollment_revision INTEGER, job_id TEXT, row_identity TEXT)")
    store = SQLiteCanonicalStore(conn)
    store.upsert("conversation_messages", message())
    conn.execute("INSERT INTO ingest_provenance_records VALUES ('imessage:7', 'e', 1, 'j', 'x')")
    store.upsert("conversation_messages", message(content="legacy rewrite"))
    assert stored(conn)["content"] == "original body"


def test_a_refused_heal_still_records_the_re_ingest(conn):
    store = SQLiteCanonicalStore(conn)
    store.upsert("conversation_messages", message(), sync_batch_id="batch-1")
    store.upsert("conversation_messages", message(dataset_id="dataset-b", content="other"), sync_batch_id="batch-2")
    assert conn.execute("SELECT sync_batch_id FROM conversation_messages").fetchone() == ("batch-2",)


# --- through a real sync ------------------------------------------------------

def test_an_imessage_sync_records_a_native_date_as_unverified(tmp_path, monkeypatch):
    """Synthetic chat.db through the real reader, parser and staging.

    An undated message never reaches staging today (the schema validator drops
    it), so the staging fill for a missing time is defensive; its mark is pinned
    at the manager level above. This pins the path that does run, and that the
    drop is still a drop rather than a row stamped with ingestion time.
    """
    from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db
    import topos.ingestion.local_sync as local_sync

    chat_db = tmp_path / "chat.db"
    source = _make_chat_db(chat_db)
    _add_message(source, rowid=1, chat_id=1, handle_id=1, text="dated")
    _add_message(source, rowid=2, chat_id=1, handle_id=1, text="undated")
    source.execute("UPDATE message SET date = NULL WHERE ROWID = 2")
    source.commit()
    source.close()
    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kwargs: None)

    node = sqlite3.connect(":memory:")
    result = local_sync.run_imessage_sync("owner:default", db_conn=node, chat_db_path=chat_db, batch_size=10,
                                          sync_options={"mode": "all"})
    assert result["status"] == "ok", result
    rows = {content: (event_at, EventTime.from_json(record)) for content, event_at, record in
            node.execute("SELECT content, event_at, event_time_json FROM conversation_messages")}
    assert set(rows) == {"dated"}
    event_at, record = rows["dated"]
    assert record.event.text == event_at and record.event.provenance == "unverified_producer"
