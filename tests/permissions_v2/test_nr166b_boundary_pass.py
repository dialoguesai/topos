"""NR166B: inside one pass, the Off-limits boundary matches a row once, and a text once (`EntityBoundary.one_pass`).

A share index's member loop matched each member's row three times with the same inputs (its dependencies, its
review context, its entity context). Inside `one_pass` the second and third answers come from memory. Pinned here:
the memory gives the answer the boundary gives without it, for the same key and the same database; a changed row, a
write on the connection, a commit seen by a later read and a refusal are each read again in full; the memory ends
with the scope on every path; another thread and a rebound copy read in full. Cache 2: a text is matched once per
reading per pass, several texts together are matched as each alone, and the reading (journal or message, a
names-only column, identifiers) is part of the key. Invented rows only.
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


# --- cache 2: one match per text per pass ------------------------------------------------------------------------------

def text_calls(monkeypatch):
    """Every text the boundary matched in full, with the reading it was matched under."""
    calls = []
    full = entity_boundary.text_hits

    def spy(text, *arguments, **keywords):
        calls.append((text, "parts" not in keywords))   # the identifier reading takes no parts
        return full(text, *arguments, **keywords)
    monkeypatch.setattr(entity_boundary, "text_hits", spy)
    return calls


@pytest.fixture
def handled(threaded):
    """An address of the protected contact's: an identifier, read in its own reading (values only, no forms)."""
    with sqlite3.connect(threaded[0].path) as db:
        db.execute("INSERT INTO contact_identifiers VALUES('protected-contact','mvell@example.org','email')")
    return threaded


def test_a_text_is_matched_once_per_pass_whoever_reads_it(handled, monkeypatch):
    conn = read(handled)
    boundary = EntityBoundary(conn)
    calls = text_calls(monkeypatch)
    with boundary.one_pass():
        assert boundary.mentions_protected("first note", "second note") is False
        assert boundary.mentions_protected("second note", "third note") is False
        assert boundary.mentions_protected("third note") is False
    names = [text for text, identifier in calls if not identifier]
    assert sorted(names) == ["first note", "second note", "third note"]
    assert len(calls) == 6   # each text once in each reading
    calls.clear()
    boundary.mentions_protected("first note")   # outside the pass, in full again
    assert len(calls) == 2


@pytest.mark.parametrize("texts", [
    ("plain words", "Mara Example called"), ("Mara Example called", "plain words"), ("plain", "more plain"),
    ("write to mvell@example.org",), ("plain", "write to mvell@example.org"), ("M.E. wrote",), ("", "plain"),
    ("Maras car", "the example", "an Example here"),
])
def test_several_texts_together_are_matched_as_each_alone(handled, texts, monkeypatch):
    conn = read(handled)
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
    plain = EntityBoundary(conn)
    expected, alone = plain.mentions_protected(*texts), [plain.mentions_protected(text) for text in texts]
    assert expected == any(alone)
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", True)
    boundary = EntityBoundary(conn)
    with boundary.one_pass():
        assert [boundary.mentions_protected(text) for text in texts] == alone
        assert boundary.mentions_protected(*texts) is expected
        assert boundary.mentions_protected(*reversed(texts)) is expected
        assert [boundary.mentions_protected(text) for text in texts] == alone


def test_the_reading_is_in_the_key(handled):
    """One text, read as a journal row reads it (a bare part anywhere), as a message does (only capitalised), in a
    names-only column, and for identifiers: four answers, never one served for another."""
    conn = read(handled)
    boundary, plain = EntityBoundary(conn), EntityBoundary(conn)
    text = "the example of it"
    readings = [lambda b: b._hits({"note": text}, True, True), lambda b: b._hits({"note": text}, True, False),
                lambda b: b._hits({"people": text}, True, False), lambda b: b._hits({"note": text}),
                lambda b: b._hits({"note": "write to mvell@example.org"}, True, False)]
    expected = [reading(plain) for reading in readings]
    assert expected[0] is True and expected[1] is False and expected[2] is True and expected[4] is True
    with boundary.one_pass():
        assert [reading(boundary) for reading in readings] == expected
        assert [reading(boundary) for reading in reversed(readings)] == list(reversed(expected))


def test_the_identifier_reading_is_not_the_name_reading(handled):
    conn = read(handled)
    boundary = EntityBoundary(conn)
    text = "write to mvell@example.org"
    with boundary.one_pass():
        assert boundary._hits({"note": text}, True, False) is True
        # the same text's name reading alone is False: it was kept under its own reading
        assert any(key[0][0] == "name" and value is False for key, value in boundary._pass.texts.items())


def test_a_row_refused_whole_keeps_nothing(handled, monkeypatch):
    conn = read(handled)
    boundary = EntityBoundary(conn)
    monkeypatch.setattr(entity_boundary, "MAX_SURFACE_BYTES", 20)
    with boundary.one_pass():
        for _ in range(2):
            with pytest.raises(PolicyError):
                boundary.mentions_protected("twelve bytes", "and twelve more")
        assert not boundary._pass.texts


def test_the_first_fault_is_met_where_it_is_met_without_the_memory(handled, monkeypatch):
    conn = read(handled)
    full = entity_boundary.text_hits

    def faulty(text, *arguments, **keywords):
        if text == "faulty":
            raise RecursionError
        return full(text, *arguments, **keywords)
    monkeypatch.setattr(entity_boundary, "text_hits", faulty)
    for texts in (("Mara Example", "faulty"), ("faulty", "Mara Example"), ("plain", "faulty")):
        monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
        expected = outcome(lambda: EntityBoundary(conn).mentions_protected(*texts))
        monkeypatch.setattr(entity_boundary, "PASS_MEMORY", True)
        boundary = EntityBoundary(conn)
        with boundary.one_pass():
            boundary.mentions_protected("plain", "Mara Example")
            assert outcome(lambda: boundary.mentions_protected(*texts)) == expected
            assert outcome(lambda: boundary.mentions_protected(*texts)) == expected


def test_another_thread_matches_texts_in_full(handled, monkeypatch):
    conn = sqlite3.connect(f"file:{handled[0].path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    boundary = EntityBoundary(conn)
    calls = text_calls(monkeypatch)
    with boundary.one_pass():
        memory = boundary._pass
        boundary.mentions_protected("a note")
        worker = threading.Thread(target=lambda: [boundary.mentions_protected("a note") for _ in range(2)])
        worker.start()
        worker.join()
        boundary.mentions_protected("a note")
        assert memory.texts
    assert len(calls) == 6 and boundary._pass is None and not memory.texts   # emptied with the scope


# --- cache 3: the two normalisers remembered per string, inside the scope only ------------------------------------------

STRINGS = ["Mara Example", "Mara Example ", "Ｍａｒａ", "MÁRA", "Mara&#32;Example", "Mara​Example", "МАRА",
           "٢١٢", "x", "x ", " x", "", "M.E.", "Æsir øre", "\U000e0041tag"]


def normaliser_calls(monkeypatch):
    calls = []
    full = entity_boundary._normalized

    def spy(value):
        calls.append(value)
        return full(value)
    monkeypatch.setattr(entity_boundary, "_normalized", spy)
    return calls


def test_a_string_is_normalised_once_per_pass_and_never_outside_one(threaded, monkeypatch):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = normaliser_calls(monkeypatch)
    assert entity_boundary._PASS_NORMALIZER.get() is None
    with boundary.one_pass():
        assert entity_boundary._PASS_NORMALIZER.get() is not None
        for _ in range(3):
            entity_boundary.normalized("Ｍａｒａ Example")
            entity_boundary.skeleton("Ｍａｒａ Example")
        assert calls == ["Ｍａｒａ Example"]
    assert entity_boundary._PASS_NORMALIZER.get() is None
    entity_boundary.normalized("Ｍａｒａ Example")
    entity_boundary.normalized("Ｍａｒａ Example")
    assert len(calls) == 3


def test_every_string_normalises_as_without_the_pass(threaded):
    conn = read(threaded)
    expected = [(entity_boundary.normalized(value), entity_boundary.skeleton(value)) for value in STRINGS]
    boundary = EntityBoundary(conn)
    with boundary.one_pass():
        for order in (STRINGS, list(reversed(STRINGS)), STRINGS):
            found = {value: (entity_boundary.normalized(value), entity_boundary.skeleton(value)) for value in order}
            assert [found[value] for value in STRINGS] == expected
        assert all(isinstance(value, str) for value in entity_boundary._PASS_NORMALIZER.get().normalized)


def test_a_value_that_is_not_a_string_is_normalised_as_without_the_pass(threaded):
    conn = read(threaded)

    class Text(str):
        pass
    boundary = EntityBoundary(conn)
    with boundary.one_pass():
        assert entity_boundary.normalized(Text("Mara")) == entity_boundary._normalized("Mara")
        with pytest.raises(TypeError):
            entity_boundary.skeleton(None)
        assert not entity_boundary._PASS_NORMALIZER.get().normalized


def test_the_normaliser_ends_with_the_scope_on_every_path(threaded):
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    with pytest.raises(PolicyError):
        with boundary.one_pass():
            memory = entity_boundary._PASS_NORMALIZER.get()
            entity_boundary.normalized("Mara Example")
            assert memory.normalized and memory.chars
            raise PolicyError("entity_protected")
    assert entity_boundary._PASS_NORMALIZER.get() is None
    assert not memory.open and not memory.normalized and not memory.skeleton and memory.chars == 0


def test_a_copied_context_and_another_thread_normalise_in_full(threaded, monkeypatch):
    import contextvars
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    calls = normaliser_calls(monkeypatch)
    with boundary.one_pass():
        entity_boundary.normalized("one")
        copied = contextvars.copy_context()   # as asyncio.to_thread and a task started inside the scope would
        worker = threading.Thread(target=lambda: copied.run(lambda: [entity_boundary.normalized("one")
                                                                     for _ in range(2)]))
        worker.start()
        worker.join()
        assert len(calls) == 3
        plain = threading.Thread(target=lambda: entity_boundary.normalized("one"))   # a fresh context: none
        plain.start()
        plain.join()
        assert len(calls) == 4
    copied.run(lambda: entity_boundary.normalized("one"))   # after the scope: closed and empty
    assert len(calls) == 5 and not copied.run(entity_boundary._PASS_NORMALIZER.get).normalized


def test_the_normaliser_stops_taking_strings_at_its_limit(threaded, monkeypatch):
    monkeypatch.setattr(entity_boundary._Normalizer, "LIMIT", 40)
    conn = read(threaded)
    boundary = EntityBoundary(conn)
    with boundary.one_pass():
        memory = entity_boundary._PASS_NORMALIZER.get()
        for value in STRINGS * 2:
            assert entity_boundary.normalized(value) == entity_boundary._normalized(value)
        assert 0 < memory.chars <= 40
        assert sum(len(key) + len(value) for key, value in memory.normalized.items()) == memory.chars


def test_a_nested_scope_shares_the_outer_normaliser_and_does_not_end_it(threaded, monkeypatch):
    conn = read(threaded)
    outer, inner = EntityBoundary(conn), EntityBoundary(conn)
    with outer.one_pass():
        memory = entity_boundary._PASS_NORMALIZER.get()
        with inner.one_pass():
            assert entity_boundary._PASS_NORMALIZER.get() is memory
            entity_boundary.normalized("shared")
        assert entity_boundary._PASS_NORMALIZER.get() is memory and memory.open and memory.normalized
    assert entity_boundary._PASS_NORMALIZER.get() is None and not memory.normalized


def test_with_the_memory_off_no_normaliser_is_set(threaded, monkeypatch):
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", False)
    conn = read(threaded)
    with EntityBoundary(conn).one_pass():
        assert entity_boundary._PASS_NORMALIZER.get() is None
