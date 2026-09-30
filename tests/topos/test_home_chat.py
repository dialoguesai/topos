"""Home chat functional storage and HTTP surface (in-memory DB)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from uuid import uuid4

import pytest
from fastapi import HTTPException

import topos.core.handlers as hub
from topos.api import home_chat as home_chat_api
from topos.control_plane_client import ControlPlaneClient
from topos.core.handlers.home_chat import (
    handle_get_home_chat_session,
    handle_upsert_home_chat_session,
)
from topos.home_chat.schema import ensure_home_chat_schema
from topos.home_chat import store


@pytest.fixture()
def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ensure_home_chat_schema(conn)
    return conn


def _sample_history() -> dict:
    return {
        "version": 3,
        "currentId": "u1",
        "messages": {
            "u1": {
                "id": "u1",
                "role": "user",
                "parentId": None,
                "childrenIds": [],
                "content": "Hi",
                "timestamp": 1,
                "done": True,
            }
        },
    }


def test_store_crud(memory_conn: sqlite3.Connection) -> None:
    sid = str(uuid4())
    store.upsert_session(
        memory_conn,
        user_id="user-1",
        payload={
            "sessionId": sid,
            "engineId": "engine-a",
            "title": "Test",
            "history": _sample_history(),
            "revision": 1,
            "createdAt": 1000,
            "updatedAt": 2000,
            "participants": [{"key": "self", "mode": "self", "label": "My Topos"}],
        },
    )
    listed = store.list_sessions(memory_conn, user_id="user-1", engine_id="engine-a")
    assert len(listed) == 1
    assert listed[0]["sessionId"] == sid
    assert listed[0]["participants"][0]["label"] == "My Topos"
    blob = store.get_session(memory_conn, user_id="user-1", session_id=sid)
    assert blob is not None
    assert blob["title"] == "Test"
    assert blob["participants"][0]["key"] == "self"
    assert store.delete_session(memory_conn, user_id="user-1", session_id=sid)
    assert store.get_session(memory_conn, user_id="user-1", session_id=sid) is None


def test_stale_revision_conflict(memory_conn: sqlite3.Connection) -> None:
    sid = str(uuid4())
    store.upsert_session(
        memory_conn,
        user_id="user-1",
        payload={
            "sessionId": sid,
            "engineId": "engine-a",
            "title": "v1",
            "history": _sample_history(),
            "revision": 2,
        },
    )
    with pytest.raises(ValueError, match="STALE_REVISION"):
        store.upsert_session(
            memory_conn,
            user_id="user-1",
            payload={
                "sessionId": sid,
                "engineId": "engine-a",
                "title": "stale",
                "history": _sample_history(),
                "revision": 1,
            },
        )


def test_cross_user_scope_hidden(memory_conn: sqlite3.Connection) -> None:
    sid = str(uuid4())
    store.upsert_session(
        memory_conn,
        user_id="user-1",
        payload={
            "sessionId": sid,
            "engineId": "engine-a",
            "title": "private",
            "history": _sample_history(),
            "revision": 1,
        },
    )
    assert store.get_session(memory_conn, user_id="user-2", session_id=sid) is None
    assert store.delete_session(memory_conn, user_id="user-2", session_id=sid) is False


# --------------------------------------- stored histories the store cannot serve
#
# `get_session` re-verifies every stored history. Until 2026-09-30 the black-hole
# rebuild wrote `[]` over each history it withdrew; every read of those sessions
# raised INVALID_HISTORY out of the relay handler ("Handler raised exception",
# 238 times in the node logs 9 to 30 Sep), and one failed read stops the web
# app's session pull, so the rest of the list never loads in a fresh tab.

#: Planted in every refused history. The refusal describes a history's shape and
#: never repeats what the owner wrote: not in the answer, not in a log line.
PLANTED = "Zq7 planted turn text"

UNVERIFIABLE = {
    "null": "null",
    "string": json.dumps(PLANTED),
    "list of turns": json.dumps([{"role": "user", "content": PLANTED}]),
    "empty object": "{}",
    "version 2": json.dumps({"version": 2, "messages": {"u1": {"content": PLANTED}}}),
    "version as text": json.dumps({"version": "3", "messages": {"u1": {"content": PLANTED}}}),
    "messages not an object": json.dumps({"version": 3, "messages": [PLANTED]}),
    "not json": "{" + PLANTED,
}


def _stored_as(conn: sqlite3.Connection, history_json: str) -> str:
    """A session whose history_json a writer other than `upsert_session` replaced."""
    sid = str(uuid4())
    store.upsert_session(
        conn,
        user_id="user-1",
        payload={
            "sessionId": sid,
            "engineId": "engine-a",
            "title": "Test",
            "history": _sample_history(),
            "revision": 1,
        },
    )
    conn.execute("UPDATE home_chat_sessions SET history_json=? WHERE id=?", (history_json, sid))
    conn.commit()
    return sid


@pytest.fixture()
def handler_conn(memory_conn, monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: memory_conn)
    return memory_conn


def _get_message(sid: str) -> dict:
    return {
        "id": "req-1",
        "type": "get_home_chat_session",
        "payload": {"user_id": "user-1", "session_id": sid},
    }


def test_the_withdrawn_marker_reads_back_as_an_empty_conversation(memory_conn):
    sid = _stored_as(memory_conn, "[]")

    blob = store.get_session(memory_conn, user_id="user-1", session_id=sid)

    assert blob is not None
    assert blob["history"] == {"version": 3, "messages": {}, "currentId": None}
    assert blob["history"] == store.empty_history()
    assert blob["title"] == "Test"


@pytest.mark.parametrize("history_json", list(UNVERIFIABLE.values()), ids=list(UNVERIFIABLE))
def test_a_stored_history_the_store_cannot_verify_is_refused(memory_conn, history_json):
    sid = _stored_as(memory_conn, history_json)

    with pytest.raises(store.InvalidHistoryError) as caught:
        store.get_session(memory_conn, user_id="user-1", session_id=sid)

    assert str(caught.value) == "INVALID_HISTORY"
    assert isinstance(caught.value, ValueError), "callers map str(ValueError) to the wire code"
    assert PLANTED not in repr(caught.value.shape)
    assert caught.value.shape["type"] in {"null", "string", "array", "object", "unparseable"}


def test_the_shape_names_types_and_an_integer_version_only():
    assert store.describe_history({"version": 2, "messages": {"u1": {"content": PLANTED}}}) == {
        "type": "object",
        "version": 2,
        "messages": "object",
        "bytes": len(json.dumps({"version": 2, "messages": {"u1": {"content": PLANTED}}}, separators=(",", ":"))),
    }
    assert store.describe_history({"version": PLANTED})["version"] == "string"
    assert store.describe_history({"version": True})["version"] == "boolean"
    assert store.describe_history([PLANTED, PLANTED])["items"] == 2


def test_get_handler_refuses_with_a_typed_error_instead_of_raising(handler_conn, caplog):
    sid = _stored_as(handler_conn, UNVERIFIABLE["version 2"])

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(handle_get_home_chat_session(_get_message(sid)))

    assert result == {
        "id": "req-1",
        "status": "error",
        "error": "INVALID_HISTORY",
        "error_code": "INVALID_HISTORY",
    }
    refused = [r for r in caplog.records if r.name == "topos.core.handlers.home_chat"]
    assert len(refused) == 1
    assert refused[0].levelno == logging.WARNING
    assert "code=INVALID_HISTORY" in refused[0].getMessage()
    assert "'version': 2" in refused[0].getMessage()
    assert PLANTED not in caplog.text


def test_get_handler_serves_the_withdrawn_marker(handler_conn):
    sid = _stored_as(handler_conn, "[]")

    result = asyncio.run(handle_get_home_chat_session(_get_message(sid)))

    assert result["status"] == "ok"
    assert result["payload"]["history"] == store.empty_history()


def test_upsert_handler_logs_the_shape_of_a_refused_history_not_its_text(handler_conn, caplog):
    message = {
        "id": "req-2",
        "type": "upsert_home_chat_session",
        "payload": {
            "user_id": "user-1",
            "session_id": str(uuid4()),
            "body": {"engineId": "engine-a", "history": {"version": 2, "messages": {"u1": {"content": PLANTED}}}},
        },
    }

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(handle_upsert_home_chat_session(message))

    assert result["error_code"] == "INVALID_HISTORY"
    assert "code=INVALID_HISTORY" in caplog.text
    assert PLANTED not in caplog.text


def test_relay_answers_the_refusal_without_the_catch_all(handler_conn, caplog):
    """The symptom as the node logged it: the relay's catch-all, not the handler, answered."""
    sid = _stored_as(handler_conn, UNVERIFIABLE["null"])
    sent: list = []

    class _Socket:
        async def send(self, message):
            sent.append(json.loads(message))

    client = ControlPlaneClient(
        control_plane_url="ws://example/ws/engine",
        api_key="test-key",
        handler=handle_get_home_chat_session,
        verify_ssl=False,
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(client._handle_message(_Socket(), _get_message(sid)))

    assert "Handler raised exception" not in caplog.text
    assert sent == [
        {
            "id": "req-1",
            "status": "error",
            "error": "INVALID_HISTORY",
            "error_code": "INVALID_HISTORY",
            "type": "get_home_chat_session",
        }
    ]


def test_http_get_refuses_with_a_400_not_a_500(memory_conn, monkeypatch):
    monkeypatch.setattr(home_chat_api, "_get_conn", lambda: memory_conn)
    sid = _stored_as(memory_conn, UNVERIFIABLE["string"])

    with pytest.raises(HTTPException) as caught:
        asyncio.run(home_chat_api.get_home_chat_session(session_id=sid, user_id="user-1"))

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "INVALID_HISTORY"
