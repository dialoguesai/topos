"""Withdrawal must reach tables that no key points at.

The lifecycle sweeps travel along keys — record_id, source_id, entity_id. Two
tables carrying the owner's prose have none of them, so every sweep walked past:

  * ``community_names`` (168 rows on the owner's node) — a community name is
    generated FROM its members, so a community the withdrawn entity belongs to
    can be named after them. Same producer relationship as a cluster label: not
    cleaning the row means the next naming pass writes the name back.
  * ``home_chat_sessions`` (104 rows) — the owner's own conversations, a title
    and a history of turns, keyed on the session. A withdrawn name sitting in a
    chat history is served back verbatim by the sessions list.

Both are kept rather than deleted, and the reasons differ. A community name row
carries ``times_matched`` and a fingerprint the namer uses to avoid re-proposing
a name it already settled on; destroying that makes the next pass re-derive the
withdrawn name from scratch. A chat session is the owner's own artifact, and
dropping turns renumbers a conversation they may be reading — an absent turn
reads as a bug, an emptied one reads as a redaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from topos.features.lifecycle.blackhole_rebuild import (
    _withdraw_community_names,
    _withdraw_home_chat_sessions,
)
from topos.home_chat import store

TERMS = {"ada lovelace", "ada"}


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    from topos.home_chat.schema import ensure_home_chat_schema

    c = sqlite3.connect(str(tmp_path / "wd.db"))
    apply_all_migrations(c)
    # The home chat store reads rows by column name.
    c.row_factory = sqlite3.Row
    # `home_chat_sessions` is created on demand by the home-chat surface rather
    # than by a migration, which is part of why no lifecycle sweep knew it
    # existed.
    ensure_home_chat_schema(c)
    yield c
    c.close()


# ------------------------------------------------------- community_names


def _community(conn, name_id, name):
    conn.execute(
        "INSERT INTO community_names (name_id, name, fingerprint_json, source, model)"
        " VALUES (?,?,?,?,?)",
        (name_id, name, "{}", "llm", "test"),
    )
    conn.commit()


def _community_rows(conn):
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT name_id, name, retired_at FROM community_names")
    }


def test_a_community_named_after_the_entity_is_retired(conn):
    _community(conn, "cmn-1", "Ada Lovelace's circle")

    assert _withdraw_community_names(conn, TERMS) == 1
    conn.commit()

    name, retired = _community_rows(conn)["cmn-1"]
    assert name == "community"
    assert retired is not None


def test_an_unrelated_community_is_untouched(conn):
    _community(conn, "cmn-2", "the climbing crew")

    assert _withdraw_community_names(conn, TERMS) == 0
    conn.commit()

    name, retired = _community_rows(conn)["cmn-2"]
    assert name == "the climbing crew"
    assert retired is None


def test_an_already_retired_community_is_not_rewritten(conn):
    """Idempotence: a second withdrawal must not churn rows it already cleaned."""
    _community(conn, "cmn-3", "Ada Lovelace's circle")
    _withdraw_community_names(conn, TERMS)
    conn.commit()

    assert _withdraw_community_names(conn, TERMS) == 0


# ---------------------------------------------------- home_chat_sessions
#
# Sessions are written and read back through the real store, which holds only
# the web app's v3 history: {"version": 3, "messages": {id: turn}, "currentId"}.
# These tests once inserted a bare list of turns, a shape the store has never
# accepted. They passed while every real withdrawal skipped the v3 walk and
# wrote `[]`, which the store refuses, so each withdrawn session failed every
# read with INVALID_HISTORY.


def _turn(turn_id, role, content, parent=None, **extra):
    return {
        "id": turn_id,
        "role": role,
        "parentId": parent,
        "childrenIds": [],
        "content": content,
        "timestamp": 1,
        "done": True,
        **extra,
    }


def _history(*turns, **top_level):
    messages = {t["id"]: t for t in turns}
    for t in turns:
        if t["parentId"]:
            messages[t["parentId"]]["childrenIds"].append(t["id"])
    current = turns[-1]["id"] if turns else None
    return {"version": 3, "messages": messages, "currentId": current, **top_level}


def _session(conn, title, history):
    sid = str(uuid.uuid4())
    store.upsert_session(
        conn,
        user_id="owner",
        payload={"sessionId": sid, "engineId": "eng", "title": title, "history": history, "revision": 1},
    )
    return sid


def _raw_session(conn, history_json):
    """A row holding a history the store would never have written."""
    sid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO home_chat_sessions (id, user_id, engine_id, title, history_json,"
        " revision, created_at_ms, updated_at_ms) VALUES (?,'owner','eng','notes',?,1,0,0)",
        (sid, history_json),
    )
    conn.commit()
    return sid


def _stored(conn, sid):
    return conn.execute(
        "SELECT title, history_json FROM home_chat_sessions WHERE id=?", (sid,)
    ).fetchone()


def _read(conn, sid):
    """Through the store, as the app reads it; raises if the withdrawal broke the row."""
    return store.get_session(conn, user_id="owner", session_id=sid)


def test_a_title_naming_the_entity_is_blanked(conn):
    sid = _session(conn, "About Ada Lovelace", _history())

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    assert _read(conn, sid)["title"] == "conversation"


def test_a_turn_naming_the_entity_is_emptied_and_the_rest_kept(conn):
    before = _history(
        _turn("u1", "user", "what did Ada Lovelace say"),
        _turn("a1", "assistant", "the weather was fine", parent="u1"),
    )
    sid = _session(conn, "notes", before)

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    history = _read(conn, sid)["history"]
    assert history["version"] == 3
    assert list(history["messages"]) == ["u1", "a1"], "turns must not be renumbered"
    assert history["messages"]["u1"]["content"] == ""
    assert history["messages"]["a1"] == before["messages"]["a1"]
    assert history["messages"]["u1"]["childrenIds"] == ["a1"]
    assert history["currentId"] == "a1"


def test_a_name_outside_the_turn_text_is_dropped(conn):
    sid = _session(conn, "notes", _history(
        _turn("u1", "user", "how was the talk"),
        _turn(
            "a1", "assistant", "it went well", parent="u1",
            error={"message": "Ada Lovelace timed out", "category": "timeout"},
            modelSwitchNotice={"text": "moved to a private model for Ada Lovelace"},
            llmModelUsed="local-model",
        ),
        summary="a chat about Ada Lovelace",
    ))

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    history = _read(conn, sid)["history"]
    turn = history["messages"]["a1"]
    assert "error" not in turn
    assert "modelSwitchNotice" not in turn
    assert turn["content"] == "it went well"
    assert turn["llmModelUsed"] == "local-model"
    assert "summary" not in history
    assert "lovelace" not in _stored(conn, sid)[1].lower()


@pytest.mark.parametrize(
    "history_json",
    [
        '{"weird": "Ada Lovelace"}',
        json.dumps([{"role": "user", "content": "what did Ada Lovelace say"}]),
        json.dumps({"version": 2, "messages": {"u1": {"content": "Ada Lovelace"}}}),
        json.dumps({"version": 3, "messages": {"u1": "Ada Lovelace"}, "currentId": "u1"}),
        "{Ada Lovelace",
    ],
    ids=["unknown object", "list of turns", "version 2", "turn not an object", "not json"],
)
def test_an_unwalkable_history_is_withheld_whole_as_an_empty_conversation(conn, history_json):
    """Fail toward withholding, in a shape the store still serves."""
    sid = _raw_session(conn, history_json)

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    assert json.loads(_stored(conn, sid)[1]) == store.empty_history()
    assert _read(conn, sid)["history"] == store.empty_history()


def test_an_unrelated_session_is_untouched(conn):
    sid = _session(conn, "grocery list", _history(_turn("u1", "user", "milk")))
    stored = _stored(conn, sid)

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    conn.commit()

    assert tuple(_stored(conn, sid)) == tuple(stored)


def test_a_name_spelled_inside_message_ids_is_not_a_mention(conn):
    """Message ids are random UUIDs; "ada" is spelled in hex letters and turns up in them."""
    u1, a1 = "5e1ada00-0000-4000-8000-000000000001", "9c0ada11-0000-4000-8000-000000000002"
    sid = _session(conn, "grocery list", _history(
        _turn(u1, "user", "milk"),
        _turn(a1, "assistant", "and bread", parent=u1),
    ))
    stored = _stored(conn, sid)
    assert "ada" in stored[1], "the fixture must put the term in the raw blob"

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    conn.commit()

    assert tuple(_stored(conn, sid)) == tuple(stored)


def test_withdrawal_is_idempotent(conn):
    sid = _session(conn, "About Ada Lovelace", _history(
        _turn("u1", "user", "tell me about Ada Lovelace"),
    ))

    _withdraw_home_chat_sessions(conn, TERMS)
    conn.commit()
    once = tuple(_stored(conn, sid))

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    assert tuple(_stored(conn, sid)) == once


def test_the_withdrawn_marker_an_earlier_rebuild_wrote_is_left_to_the_store(conn):
    """`[]` names no one, so the rebuild does not rewrite it; the store reads it as empty."""
    sid = _raw_session(conn, "[]")

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    conn.commit()

    assert _stored(conn, sid)[1] == "[]"
    assert _read(conn, sid)["history"] == store.empty_history()


def test_a_missing_table_is_not_an_error(conn):
    """Minimal databases exist; a withdrawal must not fail on their account."""
    conn.execute("DROP TABLE home_chat_sessions")
    conn.execute("DROP TABLE community_names")
    conn.commit()

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    assert _withdraw_community_names(conn, TERMS) == 0


def test_both_are_wired_into_the_rebuild(conn):
    """A helper nothing calls is not a withdrawal."""
    import inspect

    from topos.features.lifecycle import blackhole_rebuild

    src = inspect.getsource(blackhole_rebuild)
    body = src[src.index("store.mark_rebuild_running("):]
    assert "_withdraw_community_names(conn, terms)" in body
    assert "_withdraw_home_chat_sessions(conn, terms)" in body
