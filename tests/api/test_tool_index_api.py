"""HTTP tests for the tool-RAG endpoints (/v1/tools/index|retrieve|index/status)."""

from __future__ import annotations

import sqlite3
from typing import List

import pytest
from httpx import ASGITransport, AsyncClient

TOOLS = [
    {"name": "remote__topos-github__list_commits", "description": "List commits of a branch in a repository"},
    {"name": "remote__topos-github__get_me", "description": "Get my GitHub user profile"},
    {"name": "query_scope", "description": "Query synced personal data by scope"},
]


def _fake_embed(texts: List[str], input_role: str) -> List[List[float]]:
    # Axis 0 = "commit" theme, axis 1 = everything else; unit-norm.
    out: List[List[float]] = []
    for text in texts:
        vec = [1.0, 0.0] if "commit" in text.lower() else [0.0, 1.0]
        out.append(vec)
    return out


@pytest.fixture()
def wired_app(monkeypatch):
    from topos.app import app
    from topos.auth import require_api_key
    import topos.api.tool_index as api_mod
    import topos.features.signal.tool_index as core_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(api_mod, "get_db_connection", lambda: conn)
    # No model downloads in unit tests: fake the passage embedder and the
    # query-embedding path (the real one imports the HF backend).
    monkeypatch.setattr(core_mod, "_default_embed_fn", _fake_embed)
    monkeypatch.setattr(core_mod, "active_model_label", lambda: "fake-model")
    monkeypatch.setattr(
        core_mod, "_query_vector", lambda query, embed_fn=None: _fake_embed([query], "query")[0]
    )

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    try:
        yield app
    finally:
        app.dependency_overrides.pop(require_api_key, None)
        conn.close()


@pytest.mark.asyncio
async def test_tools_endpoints_require_auth():
    from topos.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path, payload in (
            ("/v1/tools/index", {"tools": TOOLS}),
            ("/v1/tools/retrieve", {"query": "commits"}),
        ):
            resp = await client.post(path, json=payload)
            assert resp.status_code in (401, 403, 422), path


@pytest.mark.asyncio
async def test_index_then_retrieve_round_trip(wired_app):
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        indexed = await client.post(
            "/v1/tools/index",
            headers={"Authorization": "Bearer test-key"},
            json={"tools": TOOLS},
        )
        assert indexed.status_code == 200
        body = indexed.json()
        assert body["indexed"] == len(TOOLS)
        assert body["total"] == len(TOOLS)

        # Idempotent re-sync: hash-deduped, nothing re-embedded.
        again = await client.post(
            "/v1/tools/index",
            headers={"Authorization": "Bearer test-key"},
            json={"tools": TOOLS},
        )
        assert again.json()["indexed"] == 0
        assert again.json()["unchanged"] == len(TOOLS)

        retrieved = await client.post(
            "/v1/tools/retrieve",
            headers={"Authorization": "Bearer test-key"},
            json={"query": "what were my most recent commits", "k": 2},
        )
        assert retrieved.status_code == 200
        out = retrieved.json()
        assert out["tools"][0]["name"] == "remote__topos-github__list_commits"
        assert "query_scope" in out["core"]

        status = await client.get(
            "/v1/tools/index/status", headers={"Authorization": "Bearer test-key"}
        )
        assert status.status_code == 200
        assert status.json()["total"] == len(TOOLS)
        assert status.json()["by_connector"]["topos-github"] == 2


@pytest.mark.asyncio
async def test_scoped_retrieve_rides_identity_tools_along(wired_app):
    # PLAN_HELP_NUDGE A2: a scoped retrieve force-includes identity-shaped
    # tools that rank below k, plus any per-request `identity_tools` override.
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        indexed = await client.post(
            "/v1/tools/index",
            headers={"Authorization": "Bearer test-key"},
            json={
                "tools": TOOLS
                + [{"name": "remote__topos-github__viewer", "description": "GraphQL viewer account"}]
            },
        )
        assert indexed.status_code == 200

        retrieved = await client.post(
            "/v1/tools/retrieve",
            headers={"Authorization": "Bearer test-key"},
            json={
                "query": "what were my most recent commits",
                "connector_scope": "topos-github",
                "k": 1,
                "identity_tools": ["viewer"],
            },
        )
        assert retrieved.status_code == 200
        out = retrieved.json()
        names = [t["name"] for t in out["tools"]]
        # k=1 keeps only the commit tool; both identity tools ride along.
        assert names[0] == "remote__topos-github__list_commits"
        assert "remote__topos-github__get_me" in names
        assert "remote__topos-github__viewer" in names
        assert set(out["identity"]) == {
            "remote__topos-github__get_me",
            "remote__topos-github__viewer",
        }
        injected = {t["name"]: t.get("injected") for t in out["tools"]}
        assert injected["remote__topos-github__get_me"] is True
        assert injected["remote__topos-github__viewer"] is True


@pytest.mark.asyncio
async def test_retrieve_requires_query_and_index_requires_tools(wired_app):
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        empty_query = await client.post(
            "/v1/tools/retrieve",
            headers={"Authorization": "Bearer test-key"},
            json={"query": "   "},
        )
        assert empty_query.status_code == 422

        empty_tools = await client.post(
            "/v1/tools/index",
            headers={"Authorization": "Bearer test-key"},
            json={"tools": []},
        )
        assert empty_tools.status_code == 422


@pytest.mark.asyncio
async def test_retrieve_observes_the_turn_on_the_direct_transport(wired_app, monkeypatch):
    """Shadow coverage must not depend on which transport the client used.

    Two entrances reach tool retrieval: the control-plane handler
    (`core/handlers/tool_index.py`, prod) and this REST route (engine-direct, dev).
    Hooking only the first would silently exclude every locally-tested turn from the
    training signal, and read later as "the model never sees dev traffic".
    """
    from topos.query import scope_shadow as ss

    seen: List[str] = []
    monkeypatch.setattr(ss, "observe_turn", lambda text, **kw: seen.append(text))

    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/v1/tools/index", json={"tools": TOOLS},
                          headers={"Authorization": "Bearer k"})
        resp = await client.post("/v1/tools/retrieve",
                                 json={"query": "how did I sleep", "k": 2},
                                 headers={"Authorization": "Bearer k"})
    assert resp.status_code == 200
    assert seen == ["how did I sleep"]


@pytest.mark.asyncio
async def test_retrieve_survives_a_broken_shadow_on_the_direct_transport(wired_app, monkeypatch):
    from topos.query import scope_shadow as ss

    def _boom(text, **kw):
        raise RuntimeError("head exploded")

    monkeypatch.setattr(ss, "observe_turn", _boom)
    transport = ASGITransport(app=wired_app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post("/v1/tools/index", json={"tools": TOOLS},
                          headers={"Authorization": "Bearer k"})
        resp = await client.post("/v1/tools/retrieve",
                                 json={"query": "anything", "k": 2},
                                 headers={"Authorization": "Bearer k"})
    assert resp.status_code == 200, "a shadow fault must not fail tool retrieval"
