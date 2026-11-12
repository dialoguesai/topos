"""`connection_info` persists the control plane's `topos_id` once — only when empty, like user_id (CONTRACTS §3)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers.device import handle_connection_info
from topos.core.state import get_engine_config_value, get_user_id


@pytest.fixture
def conn(monkeypatch, tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


def _send(payload: dict, *, req_id: str | None = "r1"):
    message = {"type": "connection_info", "payload": payload}
    if req_id is not None:
        message["id"] = req_id
    return asyncio.run(handle_connection_info(message))


def test_topos_id_is_stored_when_engine_config_has_none(conn):
    out = _send({"user_id": "user-1", "topos_id": "tp_first"})
    assert out == {"id": "r1", "status": "ok"}
    assert get_engine_config_value(conn, "topos_id") == "tp_first"
    assert get_user_id(conn) == "user-1"


def test_topos_id_is_kept_when_already_set(conn):
    _send({"user_id": "user-1", "topos_id": "tp_first"})
    _send({"user_id": "user-1", "topos_id": "tp_second"})
    assert get_engine_config_value(conn, "topos_id") == "tp_first"


def test_topos_id_is_stored_without_a_user_id(conn):
    _send({"topos_id": "tp_only"}, req_id=None)
    assert get_engine_config_value(conn, "topos_id") == "tp_only"


def test_missing_topos_id_writes_nothing(conn):
    _send({"user_id": "user-1"})
    assert get_engine_config_value(conn, "topos_id") is None


# --- local first: the first account connection moves a locally minted identity (local_identity.py)
def test_a_local_only_node_adopts_the_account_on_its_first_connection(conn):
    from topos import local_identity as li

    minted = li.ensure_local_identity(conn)
    conn.execute("CREATE TABLE messages (owner_user_id TEXT, dataset_id TEXT)")
    conn.execute("INSERT INTO messages VALUES (?, ?)", (minted["user_id"], f"{minted['user_id']}:topos:{minted['topos_id']}"))
    conn.commit()

    out = _send({"user_id": "user-1", "topos_id": "tp_first"})

    assert out == {"id": "r1", "status": "ok"}
    assert get_user_id(conn) == "user-1"
    assert get_engine_config_value(conn, "topos_id") == "tp_first"
    assert li.identity_mode(conn) == "account"
    assert tuple(conn.execute("SELECT * FROM messages").fetchone()) == ("user-1", "user-1:topos:tp_first")

    _send({"user_id": "user-2", "topos_id": "tp_second"})  # from here on the ordinary rule: never rewritten
    assert get_user_id(conn) == "user-1"
    assert get_engine_config_value(conn, "topos_id") == "tp_first"


def test_an_account_node_never_adopts_another_account(conn):
    _send({"user_id": "user-1", "topos_id": "tp_first"})
    conn.execute("CREATE TABLE messages (owner_user_id TEXT)")
    conn.execute("INSERT INTO messages VALUES ('user-1')")
    conn.commit()

    _send({"user_id": "user-2", "topos_id": "tp_second"})

    assert get_user_id(conn) == "user-1"
    assert conn.execute("SELECT owner_user_id FROM messages").fetchone()[0] == "user-1"
