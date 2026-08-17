"""ws-bridge handlers for the tool-RAG substrate (tools_index / tools_retrieve)."""

from __future__ import annotations

import sqlite3
from typing import List

import pytest


@pytest.fixture()
def wired(monkeypatch):
    import topos.core.state as state
    import topos.features.signal.tool_index as core_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(state, "db_conn", conn)
    monkeypatch.setattr(state, "_db_conn_path", None)

    def fake_embed(texts: List[str], input_role: str) -> List[List[float]]:
        return [[1.0, 0.0] if "commit" in t.lower() else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(core_mod, "_default_embed_fn", fake_embed)
    monkeypatch.setattr(core_mod, "active_model_label", lambda: "fake-model")
    monkeypatch.setattr(
        core_mod, "_query_vector", lambda query, embed_fn=None: fake_embed([query], "query")[0]
    )
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_tools_index_then_retrieve_over_ws_bridge(wired):
    from topos.core.handlers import handle_control_plane_request

    indexed = await handle_control_plane_request(
        {
            "id": "r1",
            "type": "tools_index",
            "payload": {
                "tools": [
                    {"name": "remote__topos-github__list_commits", "description": "List commits"},
                    {"name": "query_scope", "description": "Query synced data"},
                ]
            },
        }
    )
    assert indexed["status"] == "ok"
    assert indexed["payload"]["indexed"] == 2

    retrieved = await handle_control_plane_request(
        {
            "id": "r2",
            "type": "tools_retrieve",
            "payload": {"query": "recent commits", "k": 1},
        }
    )
    assert retrieved["status"] == "ok"
    assert retrieved["payload"]["tools"][0]["name"] == "remote__topos-github__list_commits"
    assert "query_scope" in retrieved["payload"]["core"]

    status = await handle_control_plane_request({"id": "r3", "type": "tools_index_status"})
    assert status["status"] == "ok"
    assert status["payload"]["total"] == 2


@pytest.mark.asyncio
async def test_tools_retrieve_scoped_injects_identity_over_ws_bridge(wired):
    # PLAN_HELP_NUDGE A2: the ws handler threads connector_scope and the
    # identity_tools override through to the ride-along logic.
    from topos.core.handlers import handle_control_plane_request

    indexed = await handle_control_plane_request(
        {
            "id": "a1",
            "type": "tools_index",
            "payload": {
                "tools": [
                    {"name": "remote__topos-github__list_commits", "description": "List commits"},
                    {"name": "remote__topos-github__get_me", "description": "My user profile"},
                    {"name": "remote__topos-github__viewer", "description": "GraphQL viewer"},
                ]
            },
        }
    )
    assert indexed["status"] == "ok"

    retrieved = await handle_control_plane_request(
        {
            "id": "a2",
            "type": "tools_retrieve",
            "payload": {
                "query": "recent commits",
                "k": 1,
                "connector_scope": "topos-github",
                "identity_tools": ["viewer"],
            },
        }
    )
    assert retrieved["status"] == "ok"
    payload = retrieved["payload"]
    names = [t["name"] for t in payload["tools"]]
    assert names[0] == "remote__topos-github__list_commits"
    assert "remote__topos-github__get_me" in names
    assert "remote__topos-github__viewer" in names
    assert set(payload["identity"]) == {
        "remote__topos-github__get_me",
        "remote__topos-github__viewer",
    }


@pytest.mark.asyncio
async def test_tools_handlers_validate_input(wired):
    from topos.core.handlers import handle_control_plane_request

    empty_tools = await handle_control_plane_request(
        {"id": "e1", "type": "tools_index", "payload": {"tools": []}}
    )
    assert empty_tools["status"] == "error"

    empty_query = await handle_control_plane_request(
        {"id": "e2", "type": "tools_retrieve", "payload": {"query": "  "}}
    )
    assert empty_query["status"] == "error"


@pytest.mark.asyncio
async def test_tools_retrieve_shadow_observes_the_turn(wired, tmp_path, monkeypatch):
    """The turn-arrival shadow hook fires here, and cannot break retrieval.

    This handler is the only per-turn event that sees the owner's text whether or not the
    turn goes on to query anything, so it is where no-query turns become visible. A
    refactor that drops the hook silently restores the blind spot that motivated it.
    """
    from topos.core.handlers import handle_control_plane_request
    from topos.query import scope_shadow as ss

    monkeypatch.setenv(ss.ENV_FLAG, "1")
    monkeypatch.setattr(ss, "default_log_path", lambda: tmp_path / "shadow.jsonl")
    seen: list[str] = []

    def _fake_observe_turn(text, **kwargs):
        seen.append(text)
        return None

    monkeypatch.setattr(ss, "observe_turn", _fake_observe_turn)

    await handle_control_plane_request(
        {"id": "s0", "type": "tools_index",
         "payload": {"tools": [{"name": "query_scope", "description": "Query synced data"}]}}
    )
    retrieved = await handle_control_plane_request(
        {"id": "s1", "type": "tools_retrieve", "payload": {"query": "how did I sleep", "k": 3}}
    )
    assert retrieved["status"] == "ok"
    assert seen == ["how did I sleep"], "the turn-arrival hook must see the raw text"


@pytest.mark.asyncio
async def test_tools_retrieve_survives_a_broken_shadow(wired, monkeypatch):
    """Telemetry may cost a millisecond, never the turn."""
    from topos.core.handlers import handle_control_plane_request
    from topos.query import scope_shadow as ss

    def _boom(text, **kwargs):
        raise RuntimeError("head exploded")

    monkeypatch.setattr(ss, "observe_turn", _boom)
    await handle_control_plane_request(
        {"id": "b0", "type": "tools_index",
         "payload": {"tools": [{"name": "query_scope", "description": "Query synced data"}]}}
    )
    retrieved = await handle_control_plane_request(
        {"id": "b1", "type": "tools_retrieve", "payload": {"query": "anything", "k": 3}}
    )
    assert retrieved["status"] == "ok", "a shadow fault must not fail tool retrieval"
