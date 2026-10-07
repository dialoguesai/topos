"""Fifth round, item 1 (second re-check, R3-M1): the row veto on a node that never made one of the message tables.

Each message table is made by its first writer: `conversation_messages` by the first messages sync,
`ai_chat_messages` by the first AI-chat write. A node that has only ever synced messages is an ordinary node, and it
has no AI-chat table at all. The boundary's row veto for the messenger stream (`legacy_veto("message_stream", …)`)
looks a row's id up in BOTH tables, and the table that was never made raised, so the row was dropped: on such a node
a routine's `get_messages` returned 0 of 10 messages, status `ok`, as soon as one contact was carried. The fourth
round exists so that an upgrade nobody asked for does not empty a routine.

The rule is the fresh-node lane's own (`evidence._copy_count`, BL-108), and the veto asks that very function: a
message table this database never made holds no row of that id, on the database's own word and nothing else (its
catalog, asked inside the read, lists nothing by the name). Every other fault still withholds: a table that is there
and cannot be read, a view by the name, a name in another letter case, a caller with no read transaction.

protects:
  - the boundary's veto over a messenger row on a store with no AI-chat table, and over an AI-chat row on a store
    with no messages table (the mirror);
  - each fault that must still withhold;
  - the real `get_messages` for a routine on a node that never made the AI-chat table.
Every person, word and id here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary

pytestmark = pytest.mark.public

NAME = "Quorra Vellaby"
MESSAGE = {"message_id": "m-1", "conversation_id": "thread-1", "source_id": "source-1", "dataset_id": "dataset-1",
           "sender_id": "owner-handle", "content": "The compiler finally builds.", "reply_to_message_id": None}
TURN = {"message_id": "t-1", "conversation_id": "chat-1", "source_id": "chat-source", "content": "How do I fix it?"}


def store(tmp_path, *, drop=(), also=()):
    """A file database with one Off-limits entry, one messenger message and one AI-chat turn, less the tables in
    `drop`; `also` are further statements. Returned open, inside a read transaction, as the veto's callers hold it."""
    path = tmp_path / "canonical.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)", (NAME.lower(), NAME, json.dumps([NAME])))
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?)", tuple(MESSAGE.values()))
    conn.execute("INSERT INTO ai_chat_messages VALUES(?,?,?,?)", tuple(TURN.values()))
    for table in drop:
        conn.execute(f"DROP TABLE {table}")
    for statement in also:
        conn.execute(statement)
    conn.commit()
    conn.close()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    reader.execute("BEGIN")
    return reader


def veto(conn, row):
    return EntityBoundary(conn).legacy_veto("message_stream", dict(row))


def test_a_messenger_row_is_judged_on_a_store_that_never_made_the_ai_chat_table(tmp_path):
    """Rule: the veto reads a message table this database never made as holding no row of that id
    (`EntityBoundary._never_made`, which asks `evidence._copy_count`). Without it the lookup raises and every row of
    the stream is withheld."""
    conn = store(tmp_path, drop=("ai_chat_messages", "ai_chat_conversations"))
    assert veto(conn, MESSAGE) is False                                   # judged, and it names nobody
    named = {**MESSAGE, "content": "Lunch with Quorra Vellaby went late."}
    assert veto(conn, named) is True                                      # judged, and it names the person


def test_an_ai_chat_row_is_judged_on_a_store_that_never_made_the_messages_table(tmp_path):
    """The mirror: a node whose only data is an AI-chat import."""
    conn = store(tmp_path, drop=("conversation_messages",))
    assert veto(conn, TURN) is False
    assert veto(conn, {**TURN, "content": "Draft a note to Quorra Vellaby."}) is True


def test_with_both_tables_nothing_changed(tmp_path):
    conn = store(tmp_path)
    assert veto(conn, MESSAGE) is False and veto(conn, TURN) is False
    with pytest.raises(PolicyError):                                      # an id neither table holds: as before
        veto(conn, {**MESSAGE, "message_id": "m-none"})


@pytest.mark.parametrize("fault", [
    ("ai_chat_messages",), ("ai_chat_messages", "ai_chat_conversations"),
], ids=["a_view_by_the_name", "a_name_in_another_letter_case"])
def test_a_table_that_is_there_in_some_other_shape_still_withholds(tmp_path, fault):
    """The catalog lists something by the name: that is not "never made". The row is withheld, as before."""
    if len(fault) == 1:
        also = ("CREATE VIEW ai_chat_messages AS SELECT 1 AS n",)
    else:
        also = ("CREATE TABLE Ai_Chat_Messages(message_id TEXT, conversation_id TEXT, source_id TEXT, content TEXT)",)
    conn = store(tmp_path, drop=("ai_chat_messages",), also=also)
    with pytest.raises(PolicyError):
        veto(conn, MESSAGE)


def test_a_table_that_is_there_and_lacks_a_column_still_withholds(tmp_path):
    conn = store(tmp_path, also=("ALTER TABLE ai_chat_messages RENAME COLUMN content TO body",))
    with pytest.raises(PolicyError):
        veto(conn, MESSAGE)


def test_with_no_read_transaction_nothing_is_taken_on_the_catalogs_word(tmp_path):
    """The other lane's rule: outside a read transaction the catalog could be a later one than the read saw. The
    veto's callers hold one (`BlackholeGuard.filter_observed_canonical_rows` opens its own snapshot)."""
    conn = store(tmp_path, drop=("ai_chat_messages", "ai_chat_conversations"))
    conn.rollback()
    assert not conn.in_transaction
    with pytest.raises(PolicyError):
        veto(conn, MESSAGE)


def test_neither_table_is_still_no_row(tmp_path):
    conn = store(tmp_path, drop=("ai_chat_messages", "ai_chat_conversations", "conversation_messages"))
    with pytest.raises(PolicyError):
        veto(conn, MESSAGE)


def test_the_veto_asks_the_other_lanes_own_function(tmp_path, monkeypatch):
    """Not a second rule: the answer "never made" is `evidence._copy_count`'s. Make that function refuse and the row
    is withheld again."""
    from topos.permissions_v2 import evidence

    conn = store(tmp_path, drop=("ai_chat_messages", "ai_chat_conversations"))
    asked = []
    real = evidence._copy_count

    def count(connection, table, content):
        asked.append(table)
        return real(connection, table, content)
    monkeypatch.setattr(evidence, "_copy_count", count)
    assert veto(conn, MESSAGE) is False and asked == ["ai_chat_messages"]

    def refuses(connection, table, content):
        raise sqlite3.OperationalError("no such table: " + table)
    monkeypatch.setattr(evidence, "_copy_count", refuses)
    with pytest.raises(PolicyError):
        veto(conn, MESSAGE)
