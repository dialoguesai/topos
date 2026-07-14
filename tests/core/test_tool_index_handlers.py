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
