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
