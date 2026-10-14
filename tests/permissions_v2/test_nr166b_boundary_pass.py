"""NR166B: inside one pass, the Off-limits boundary matches a row once (`EntityBoundary.one_pass`).

A share index's member loop matched each member's row three times with the same inputs (its dependencies, its
review context, its entity context). Inside `one_pass` the second and third answers come from memory. Pinned here:
the memory gives the answer the boundary gives without it, for the same key and the same database; a changed row, a
write on the connection, a commit seen by a later read and a refusal are each read again in full; the memory ends
with the scope on every path; another thread and a rebound copy read in full. Invented rows only.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.permissions_v2.test_evidence import corpus  # noqa: F401
from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401
from topos.permissions_v2 import entity_boundary
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary, _observed_key

BENIGN = "I enjoy reading history books."
PROTECTED = "Mara Example called."


@pytest.fixture
def threaded(protected_corpus):
    """A second message in the thread, which message-1 replies to: its text is read as message-1's context."""
    with sqlite3.connect(protected_corpus[0].path) as db:
        db.execute("INSERT INTO conversation_messages(message_id,dataset_id,source_id,content,is_from_self,owner_user_id,"
                   "conversation_id,sender_id,metadata_json) VALUES('other','dataset-1','source-1',?,1,'owner-1',"
                   "'thread-1','owner-handle','{}')", (BENIGN,))
        db.execute("UPDATE conversation_messages SET reply_to_message_id='other' WHERE message_id='message-1'")
    return protected_corpus


def read(corpus, *, writable=False):
    path = corpus[0].path
    conn = sqlite3.connect(path) if writable else sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def message(conn, message_id="message-1"):
    return dict(conn.execute("SELECT * FROM conversation_messages WHERE message_id=?", (message_id,)).fetchone())


def ask(boundary, row, **identity):
    return boundary.observe(**{"table": "conversation_messages", "record_id": row.get("message_id"),
                               "source_id": row.get("source_id"), "dataset_id": row.get("dataset_id"), **identity},
                            row=row)


def counted(boundary):
    """How many times this boundary matched in full."""
    calls = []
    full = boundary._observe

    def spy(**kwargs):
        calls.append(kwargs["record_id"])
        return full(**kwargs)
    boundary._observe = spy
    return calls


def outcome(call):
    try:
        return ("answer", call())
    except PolicyError as exc:
        return ("refused", exc.code)
    except Exception as exc:  # noqa: BLE001 -- the type is the outcome being compared
        return ("raised", type(exc).__name__)


def test_a_pass_matches_a_row_once_and_gives_the_answer_it_gives_without_the_pass(threaded):
    conn = read(threaded)
    plain, boundary = EntityBoundary(conn), EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    expected = ask(plain, dict(row))
    with boundary.one_pass():
        answers = [ask(boundary, dict(row)) for _ in range(3)]
    assert answers == [expected] * 3 and len(calls) == 1
    assert ask(boundary, dict(row)) == expected and len(calls) == 2   # outside the pass, every call in full


def test_a_changed_row_is_matched_again(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    with boundary.one_pass():
        assert ask(boundary, row)[0] is False
        assert ask(boundary, {**row, "content": PROTECTED})[0] is True
        assert ask(boundary, {**row, "content": "M.E. called."})[0] is True
        assert ask(boundary, row)[0] is False
        assert len(calls) == 3
        reordered = dict(reversed(list(row.items())))
        assert ask(boundary, reordered) == ask(boundary, row) and len(calls) == 4   # another order is another key
        assert ask(boundary, {**row, "is_from_self": True}) == ask(boundary, row) and len(calls) == 5   # another type


def test_another_identity_is_another_key(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    with boundary.one_pass():
        for identity in ({}, {"record_id": "message-x"}, {"dataset_id": "dataset-x"}, {"source_id": "source-x"}):
            outcome(lambda: ask(boundary, row, **identity))
    assert len(calls) == 4


def test_the_key_leaves_out_only_the_columns_observe_never_reads(threaded):
    conn = read(threaded)
    row = message(conn)
    key = _observed_key("conversation_messages", "message-1", "source-1", "dataset-1", row)
    for column in row:
        assert _observed_key("conversation_messages", "message-1", "source-1", "dataset-1",
                             {**row, column: f"{row[column]}x"}) != key, column
    unread = {**row, "_p2b_source_revision": "a" * 64, "_p2b_native_classification": None}
    assert _observed_key("conversation_messages", "message-1", "source-1", "dataset-1", unread) == key
    # ...and they are one answer, read in full both ways (keyed_surfaces passes over them, nothing else reads them).
    assert ask(EntityBoundary(conn), unread) == ask(EntityBoundary(conn), row)


def test_a_refusal_is_not_remembered(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = {**message(conn), "conversation_id": "no-such-thread"}
    with boundary.one_pass():
        for _ in range(2):
            with pytest.raises(PolicyError):
                ask(boundary, row)
        assert len(calls) == 2 and not boundary._pass.observed


def test_a_write_by_the_connection_is_read_again(threaded):
    """The reply it answers is changed on the boundary's own connection inside its transaction: total_changes moves."""
    conn = read(threaded, writable=True)
    boundary = EntityBoundary(conn)
    row = message(conn)
    with boundary.one_pass():
        assert ask(boundary, row)[0] is False
        conn.execute("UPDATE conversation_messages SET content=? WHERE message_id='other'", (PROTECTED,))
        assert ask(boundary, row)[0] is True
    conn.rollback()


def test_a_commit_seen_by_a_later_read_is_read_again(threaded):
    """The read ends, another connection commits, a new read begins on the same connection: data_version moves."""
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    with boundary.one_pass():
        assert ask(boundary, row)[0] is False
        conn.execute("COMMIT")
        assert ask(boundary, row)[0] is False and ask(boundary, row)[0] is False
        assert len(calls) == 3   # outside a transaction there is no token, and nothing is remembered
        with sqlite3.connect(threaded[0].path) as other:
            other.execute("UPDATE conversation_messages SET content=? WHERE message_id='other'", (PROTECTED,))
        conn.execute("BEGIN")
        assert ask(boundary, row)[0] is True


def test_a_commit_during_the_read_is_not_seen_with_or_without_the_pass(threaded):
    """Inside one read nothing another connection commits is visible, so nothing is read again: both ways agree."""
    with sqlite3.connect(threaded[0].path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    conn = read(threaded)
    boundary, plain = EntityBoundary(conn), EntityBoundary(conn)
    row = message(conn)
    with boundary.one_pass():
        assert ask(boundary, row)[0] is False
        with sqlite3.connect(threaded[0].path) as other:
            other.execute("UPDATE conversation_messages SET content=? WHERE message_id='other'", (PROTECTED,))
        assert ask(boundary, row) == ask(plain, row) and ask(plain, row)[0] is False


def test_the_memory_ends_with_the_scope_on_every_path(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    row = message(conn)
    with pytest.raises(PolicyError, match="entity_protected"):
        with boundary.one_pass():
            memory = boundary._pass
            ask(boundary, row)
            assert memory.observed
            boundary.check(table="conversation_messages", record_id="message-1", source_id="source-1",
                           dataset_id="dataset-1", row={**row, "content": PROTECTED})
    assert boundary._pass is None and not memory.open and not memory.observed
    with boundary.one_pass():
        memory = boundary._pass
        ask(boundary, row)
    assert boundary._pass is None and not memory.open and not memory.observed


def test_a_nested_scope_leaves_the_outer_one_to_clear(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    with boundary.one_pass():
        outer = boundary._pass
        with boundary.one_pass():
            assert boundary._pass is outer
            ask(boundary, row)
        assert boundary._pass is outer and outer.observed
        ask(boundary, row)
    assert len(calls) == 1 and not outer.observed


def test_another_thread_reads_in_full(threaded):
    conn = sqlite3.connect(f"file:{threaded[0].path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    boundary = EntityBoundary(conn)
    calls = counted(boundary)
    row = message(conn)
    answers = []
    with boundary.one_pass():
        ask(boundary, row)
        worker = threading.Thread(target=lambda: answers.extend(ask(boundary, row) for _ in range(2)))
        worker.start()
        worker.join()
        assert ask(boundary, row) == answers[0] == answers[1]
    assert len(calls) == 3


def test_a_rebound_copy_starts_outside_the_pass(threaded):
    conn, again = read(threaded), read(threaded)
    boundary = EntityBoundary(conn)
    row = message(conn)
    with boundary.one_pass():
        ask(boundary, row)
        copy = boundary.rebind(again)
        assert copy._pass is None
        calls = counted(copy)
        ask(copy, row)
        assert len(calls) == 1


def test_an_off_limits_change_is_another_boundary(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    row = {**message(conn), "content": "Ann called."}
    with boundary.one_pass():
        assert ask(boundary, row)[0] is False
        conn.execute("COMMIT")
        with sqlite3.connect(threaded[0].path) as other:
            other.execute("UPDATE entities SET aliases_json='[\"Ann\"]' WHERE entity_id='protected-entity'")
            other.execute("UPDATE entity_blackholes SET aliases_json='[\"Ann\"]'")
        conn.execute("BEGIN")
        assert ask(EntityBoundary(conn), row)[0] is True   # the new list is a new closure
        # The old closure is the one this boundary was built with, with or without the pass.
        assert ask(boundary, row)[0] is False


@pytest.mark.parametrize("value", [b"bytes", float("nan"), ["a", "list"], {"a": "dict"}])
def test_a_row_the_key_cannot_hold_is_answered_as_without_the_pass(threaded, value):
    conn = read(threaded)
    boundary, plain = EntityBoundary(conn), EntityBoundary(conn)
    row = {**message(conn), "metadata_json": value}
    with boundary.one_pass():
        first = outcome(lambda: ask(boundary, row))
        assert first == outcome(lambda: ask(boundary, row)) == outcome(lambda: ask(plain, row))
        assert not boundary._pass.observed


@pytest.mark.parametrize("content", [BENIGN, PROTECTED, "M.E. called.", "Mara​ Example", "Maras car is here.",
                                     "Ask M.E. tomorrow.", "mara example", "Example of a meeting."])
@pytest.mark.parametrize("table", ["conversation_messages", "journal_entries", "signal_objects"])
def test_every_answer_in_a_pass_equals_the_answer_without_it(threaded, content, table, monkeypatch):
    conn = read(threaded)
    row = {**message(conn), "content": content, "payload_json": "{}"}
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
    plain = EntityBoundary(conn)
    with plain.one_pass():
        assert plain._pass is None   # with the memory off a pass remembers nothing
        expected = outcome(lambda: ask(plain, row, table=table))
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", True)
    boundary = EntityBoundary(conn)
    with boundary.one_pass():
        assert [outcome(lambda: ask(boundary, row, table=table)) for _ in range(3)] == [expected] * 3
