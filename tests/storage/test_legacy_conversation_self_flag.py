"""The legacy conversation_messages insert stores the owner only for a typed flag.

Its values are coerced with truthiness, and declared field maps hand it text, so
"0" and "false" were stored as the owner's own messages.
"""
from __future__ import annotations

import sqlite3

import pytest

from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    connection = sqlite3.connect(str(tmp_path_factory.mktemp("legacy_self_flag") / "canonical.db"))
    apply_all_migrations(connection)
    ConversationsTablesManager(connection).ensure_tables()
    yield connection
    connection.close()


def _stored_flag(conn, key, value):
    message_id = f"m:{key}:{type(value).__name__}:{value!r}"
    SQLiteCanonicalStore(conn).upsert("conversation_messages", {
        "message_id": message_id,
        "conversation_id": "c1",
        "dataset_id": "owner:default",
        "event_at": "2026-09-01T10:00:00+00:00",
        "sender_type": "human",
        "sender_id": "+15555550142",
        "content": "I work at Ferrograph Instruments",
        "source_id": "declared_source",
        key: value,
    })
    return conn.execute("SELECT is_from_self FROM conversation_messages WHERE message_id=?", (message_id,)).fetchone()[0]


@pytest.mark.parametrize("key", ["is_from_self", "from_self"])
@pytest.mark.parametrize("value", ["0", "false", "False", "no", "1", "true", 1.0, [1]])
def test_untyped_flags_are_not_the_owner(conn, key, value):
    assert _stored_flag(conn, key, value) == 0


@pytest.mark.parametrize("key", ["is_from_self", "from_self"])
@pytest.mark.parametrize("value", [1, True])
def test_typed_flags_are_the_owner(conn, key, value):
    assert _stored_flag(conn, key, value) == 1


@pytest.mark.parametrize("value", [0, False, None])
def test_typed_negatives_stay_not_the_owner(conn, value):
    assert _stored_flag(conn, "is_from_self", value) == 0
