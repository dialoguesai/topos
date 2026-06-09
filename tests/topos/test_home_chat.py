"""Home chat functional storage and HTTP surface (in-memory DB)."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

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
        },
    )
    listed = store.list_sessions(memory_conn, user_id="user-1", engine_id="engine-a")
    assert len(listed) == 1
    assert listed[0]["sessionId"] == sid
    blob = store.get_session(memory_conn, user_id="user-1", session_id=sid)
    assert blob is not None
    assert blob["title"] == "Test"
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
