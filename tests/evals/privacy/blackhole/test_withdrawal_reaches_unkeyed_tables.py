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

import pytest

from topos.features.lifecycle.blackhole_rebuild import (
    _withdraw_community_names,
    _withdraw_home_chat_sessions,
)

TERMS = {"ada lovelace", "ada"}


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    from topos.home_chat.schema import ensure_home_chat_schema

    c = sqlite3.connect(str(tmp_path / "wd.db"))
    apply_all_migrations(c)
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


def _session(conn, sid, title, turns):
    conn.execute(
        "INSERT INTO home_chat_sessions (id, user_id, engine_id, title, history_json,"
        " revision, created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,1,0,0)",
        (sid, "owner", "eng", title, json.dumps(turns)),
    )
    conn.commit()


def _session_row(conn, sid):
    return conn.execute(
        "SELECT title, history_json FROM home_chat_sessions WHERE id=?", (sid,)
    ).fetchone()


def test_a_title_naming_the_entity_is_blanked(conn):
    _session(conn, "s1", "About Ada Lovelace", [])

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    assert _session_row(conn, "s1")[0] == "conversation"


def test_a_turn_naming_the_entity_is_emptied_and_the_rest_kept(conn):
    _session(conn, "s2", "notes", [
        {"role": "user", "content": "what did Ada Lovelace say"},
        {"role": "assistant", "content": "the weather was fine"},
    ])

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    turns = json.loads(_session_row(conn, "s2")[1])
    assert len(turns) == 2, "turns must not be renumbered"
    assert turns[0]["content"] == ""
    assert turns[1]["content"] == "the weather was fine"


def test_an_unwalkable_history_is_withheld_whole(conn):
    """Fail toward withholding. A shape we cannot walk must not be served."""
    conn.execute(
        "INSERT INTO home_chat_sessions (id, user_id, engine_id, title, history_json,"
        " revision, created_at_ms, updated_at_ms)"
        " VALUES ('s3','owner','eng','notes','{\"weird\": \"Ada Lovelace\"}',1,0,0)"
    )
    conn.commit()

    assert _withdraw_home_chat_sessions(conn, TERMS) == 1
    conn.commit()

    assert _session_row(conn, "s3")[1] == "[]"


def test_an_unrelated_session_is_untouched(conn):
    _session(conn, "s4", "grocery list", [{"role": "user", "content": "milk"}])

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0
    conn.commit()

    title, history = _session_row(conn, "s4")
    assert title == "grocery list"
    assert json.loads(history)[0]["content"] == "milk"


def test_withdrawal_is_idempotent(conn):
    _session(conn, "s5", "About Ada Lovelace", [
        {"role": "user", "content": "tell me about Ada Lovelace"},
    ])

    _withdraw_home_chat_sessions(conn, TERMS)
    conn.commit()

    assert _withdraw_home_chat_sessions(conn, TERMS) == 0


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
