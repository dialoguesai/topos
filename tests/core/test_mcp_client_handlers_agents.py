"""The relay handlers behind the web app's one Connected-apps list (L3 + L10).

protects: `mcp_client_list` returns third-party rows AND `owner_agent` rows,
each saying its principal_class; `mcp_client_revoke` of an `agent-*` id runs
the agent Disconnect (revoke_agent, pause every transcript source through the
§2.8 door, remove the label file) and never touches `revoke_client`; a
third-party id keeps today's path and never touches `revoke_agent`.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.public

import topos.core.handlers as hub  # noqa: E402
from tests.topos.fake_node_socket import FakeNode, serve_fake_node  # noqa: E402
from topos import mcp_clients  # noqa: E402
from topos.core.handlers import handle_control_plane_request  # noqa: E402
from topos.principal import OWNER_APP, Principal  # noqa: E402

OWNER = Principal(cls=OWNER_APP, channel="uds")
LABEL = "tpa_claude-code." + "ab" * 20
AGENT_ROW = {
    "client": "claude-code", "client_id": "agent-claude-code", "display_name": "Claude Code",
    "principal_class": "owner_agent", "created_at": "2026-10-15T21:00:00+00:00",
    "last_used_at": None, "revoked_at": None,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    agents = tmp_path / ".topos" / "agents"
    agents.mkdir(parents=True, mode=0o700)
    (agents / "claude-code.token").write_text(LABEL + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


@pytest.fixture
def registry(monkeypatch):
    calls = {"revoke_agent": [], "revoke_client": []}
    rows = [dict(AGENT_ROW)]

    def list_agents(conn):
        return [dict(r) for r in rows]

    def revoke_agent(conn, *, client):
        calls["revoke_agent"].append(client)
        for r in rows:
            if r["client"] == client and not r["revoked_at"]:
                r["revoked_at"] = "2026-10-15T22:00:00+00:00"
                return True
        return False

    real_revoke_client = mcp_clients.revoke_client

    def revoke_client(conn, client_id):
        calls["revoke_client"].append(client_id)
        return real_revoke_client(conn, client_id)

    monkeypatch.setattr(mcp_clients, "list_agents", list_agents, raising=False)
    monkeypatch.setattr(mcp_clients, "revoke_agent", revoke_agent, raising=False)
    monkeypatch.setattr(mcp_clients, "revoke_client", revoke_client)
    return calls


def _socket_path() -> Path:
    return Path(os.environ["TOPOS_UDS_PATH"])


@pytest.mark.asyncio
async def test_list_carries_both_kinds_with_principal_class(conn, registry):
    mcp_clients.mint_client_token(conn, client_id="claude-desktop", display_name="Claude Desktop")
    out = await handle_control_plane_request({"id": "1", "type": "mcp_client_list", "payload": {}}, principal=OWNER)
    assert out["status"] == "ok"
    by_id = {c["client_id"]: c for c in out["payload"]["clients"]}
    assert set(by_id) == {"claude-desktop", "agent-claude-code"}
    assert by_id["claude-desktop"]["principal_class"] == "third_party"
    assert by_id["agent-claude-code"]["principal_class"] == "owner_agent"
    assert by_id["agent-claude-code"]["display_name"] == "Claude Code"
    assert by_id["agent-claude-code"]["created_at"] == "2026-10-15T21:00:00+00:00"
    assert by_id["agent-claude-code"]["revoked_at"] is None
    for row in by_id.values():
        assert "token_hash" not in row and "label" not in row


@pytest.mark.asyncio
async def test_revoke_agent_id_runs_the_disconnect(conn, registry, home):
    node = FakeNode()
    with serve_fake_node(node, _socket_path()):
        out = await handle_control_plane_request(
            {"id": "2", "type": "mcp_client_revoke", "payload": {"client_id": "agent-claude-code"}},
            principal=OWNER,
        )
    assert out["status"] == "ok"
    assert out["payload"]["revoked"] is True
    assert registry["revoke_agent"] == ["claude-code"]
    assert registry["revoke_client"] == []
    paused = sorted((c["source_id"], c["action"]) for c in node.calls if c["route"] == "door")
    assert paused == [
        ("claude_code_sessions", "pause"), ("codex_sessions", "pause"), ("cursor_sessions", "pause"),
    ]
    assert not (home / ".topos" / "agents" / "claude-code.token").exists()


@pytest.mark.asyncio
async def test_revoke_third_party_id_is_unchanged(conn, registry, home):
    mcp_clients.mint_client_token(conn, client_id="claude-desktop")
    out = await handle_control_plane_request(
        {"id": "3", "type": "mcp_client_revoke", "payload": {"client_id": "claude-desktop"}},
        principal=OWNER,
    )
    assert out["status"] == "ok" and out["payload"]["revoked"] is True
    assert registry["revoke_client"] == ["claude-desktop"]
    assert registry["revoke_agent"] == []
    assert (home / ".topos" / "agents" / "claude-code.token").exists()
