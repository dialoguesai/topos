"""`/v1/shell/status` additive fields for `topos-node status` (CONTRACTS.md §4, PLAN §2.2).

protects: `control_plane` is derived from the client's connection state with
the rejected-key case named; `owner_key_persisted` answers from the env file
without ever returning the key; identity presence is a bool, never a value.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.public

from topos.api import shell as shell_module  # noqa: E402
from topos.core import state  # noqa: E402

OWNER_KEY = "ok_" + "injected" + "0123456789abcdef" * 3


class _CpClient:
    def __init__(self, status):
        self._status = status

    def get_connection_status(self):
        return dict(self._status)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(shell_module._apply_state, "applying", False)
    monkeypatch.setitem(shell_module._apply_state, "last_result", None)
    monkeypatch.setattr(shell_module, "get_update_info", lambda: None)
    app = FastAPI()
    app.include_router(shell_module.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "status, expected",
    [
        (None, "unknown"),
        ({"state": "connected", "connected": True, "last_failure_category": "none"}, "connected"),
        ({"state": "degraded", "connected": False, "last_failure_category": "auth"}, "rejected"),
        ({"state": "backing_off", "connected": False, "last_failure_category": "auth"}, "rejected"),
        ({"state": "degraded", "connected": False, "last_failure_category": "protocol"}, "degraded"),
        ({"state": "backing_off", "connected": False, "last_failure_category": "network"}, "offline"),
        ({"state": "connecting", "connected": False, "last_failure_category": "none"}, "offline"),
        ({"state": "idle", "connected": False, "last_failure_category": "none"}, "offline"),
    ],
)
def test_control_plane_state_mapping(status, expected):
    assert shell_module._control_plane_state(status) == expected


def test_status_carries_the_additive_fields(client, monkeypatch, tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(f"TOPOS_KEY=abc\nTOPOS_OWNER_KEY={OWNER_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("TOPOS_ENV_FILE", str(env))
    monkeypatch.setattr(
        state, "control_plane_client",
        _CpClient({"state": "degraded", "connected": False, "last_failure_category": "auth"}),
    )
    # The TestClient serves the route on a portal thread; the node's own
    # connection is thread-bound to the serving thread, this stand-in is not.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO engine_config (key, value) VALUES ('user_id', 'user-1')")
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)

    response = client.get("/v1/shell/status")
    payload = response.json()
    assert payload["control_plane"] == "rejected"
    assert payload["owner_key_persisted"] is True
    assert payload["user_id_present"] is True
    assert payload["topos_id_present"] is False
    assert OWNER_KEY not in response.text
    assert "user-1" not in response.text
    # The pre-existing keys are untouched.
    assert payload["name"] == "topos-node" and payload["update"]["available"] is False


def test_owner_key_not_persisted_without_the_line(client, monkeypatch, tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("TOPOS_KEY=abc\n", encoding="utf-8")
    monkeypatch.setenv("TOPOS_ENV_FILE", str(env))
    monkeypatch.setattr(state, "control_plane_client", None)
    monkeypatch.setattr(state, "get_db_connection", lambda: None)
    payload = client.get("/v1/shell/status").json()
    assert payload["owner_key_persisted"] is False
    assert payload["control_plane"] == "unknown"
    assert payload["user_id_present"] is False and payload["topos_id_present"] is False
