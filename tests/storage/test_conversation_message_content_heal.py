"""A re-ingest must be able to correct a message body it previously got wrong.

`conversation_messages` was the one canonical table whose upsert wrote `content`
on INSERT and never again: an existing row only had `sync_batch_id` and
`ingested_at` touched. Every other canonical table (ai_chat_messages,
activity_events, ...) carries content in its DO UPDATE set.

That asymmetry is what made the iMessage `attributedBody` decode fix unable to
reach rows already on disk. Re-syncing chat.db read those messages correctly and
then dropped the corrected text at the write, leaving 3,722 rows -- 49% of the
owner's iMessage corpus on 2026-08-28 -- holding NSAttributedString archive bytes.
"""

import sqlite3

import pytest

from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "heal.db"))
    apply_all_migrations(conn)
    ensure_all_tables(conn)
    yield SQLiteCanonicalStore(conn)
    conn.close()


def _record(content, **over):
    rec = {
        "message_id": "imessage:88012",
        "conversation_id": "34",
        "dataset_id": "ds",
        "event_at": "2026-08-01T00:00:00Z",
        "sender_type": "human",
        "sender_id": "self",
        "content": content,
        "source_id": "imessage",
    }
    rec.update(over)
    return rec


def _read(store, message_id="imessage:88012"):
    return store._conn.execute(
        """SELECT content, content_disclosure, content_disclosure_hash, content_hash
           FROM conversation_messages WHERE message_id=?""",
        (message_id,),
    ).fetchone()


def test_reingest_replaces_a_body_the_reader_got_wrong(store):
    """The whole point: the corrected decode reaches an existing row."""
    store.upsert("conversation_messages", _record("streamtyped"))
    assert _read(store)[0] == "streamtyped"

    store.upsert("conversation_messages", _record("can you send me that link again when you get a chance"))
    assert _read(store)[0] == "can you send me that link again when you get a chance"


def test_healing_clears_the_scrub_of_the_old_body(store):
    """`content_disclosure` describes text that no longer exists once content moves."""
    store.upsert("conversation_messages", _record("streamtyped"))
    store._conn.execute(
        """UPDATE conversation_messages
           SET content_disclosure='streamtyped', content_disclosure_hash='abc',
               content_disclosure_model='m1', content_hash='deadbeef'
           WHERE message_id='imessage:88012'"""
    )
    store._conn.commit()

    store.upsert("conversation_messages", _record("the real message"))
    content, disclosure, disclosure_hash, content_hash = _read(store)
    assert content == "the real message"
    assert disclosure is None, "a scrub of the discarded body must not survive it"
    assert disclosure_hash is None
    assert content_hash is None


def test_unchanged_content_leaves_derived_columns_alone(store):
    """A routine re-sync must not invalidate the whole corpus every night."""
    store.upsert("conversation_messages", _record("stable body"))
    store._conn.execute(
        """UPDATE conversation_messages SET content_disclosure='stable body',
           content_disclosure_model='m1' WHERE message_id='imessage:88012'"""
    )
    store._conn.commit()

    store.upsert("conversation_messages", _record("stable body"))
    content, disclosure, _, _ = _read(store)
    assert content == "stable body"
    assert disclosure == "stable body", "an unchanged body must not trigger a re-scrub"


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_incoming_body_never_erases_a_stored_one(store, empty):
    """A source that stops reporting text must not blank the archive."""
    store.upsert("conversation_messages", _record("real text that exists"))
    store.upsert("conversation_messages", _record(empty))
    assert _read(store)[0] == "real text that exists"


def test_insert_still_reports_created_once(store):
    """The created/updated signal drives sync counters; healing must not skew it."""
    assert store.upsert("conversation_messages", _record("first")).created is True
    assert store.upsert("conversation_messages", _record("second")).created is False
