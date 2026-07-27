"""Topos app auth: protected routes 401 without/wrong Bearer, 200 with valid key (deployment stability)."""

import sys

import pytest
from httpx import ASGITransport, AsyncClient
from topos.testing.lifespan import LifespanManager


def load_topos_app(monkeypatch, env: dict):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for mod in (
        "topos.app",
        "topos.config.settings",
        "topos.auth",
        "topos.core.state",
    ):
        sys.modules.pop(mod, None)
    from topos.app import app  # noqa: E402
    return app


@pytest.mark.asyncio
async def test_local_mcp_401_without_auth(monkeypatch, tmp_path):
    """POST /api/local/list_database_tables returns 401 when Authorization is missing."""
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "TOPOS_CONTROL_PLANE_URL": "",
            "TOPOS_DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/local/list_database_tables")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_local_mcp_401_wrong_bearer(monkeypatch, tmp_path):
    """POST /api/local/list_database_tables returns 401 when Bearer token is wrong."""
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "TOPOS_CONTROL_PLANE_URL": "",
            "TOPOS_DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/local/list_database_tables",
                headers={"Authorization": "Bearer wrong-key"},
            )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_local_mcp_200_with_valid_bearer(monkeypatch, tmp_path):
    """POST /api/local/list_database_tables returns 200 with valid Bearer (body may be error if no DB)."""
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "TOPOS_CONTROL_PLANE_URL": "",
            "TOPOS_DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/local/list_database_tables",
                headers={"Authorization": "Bearer test-key"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert "tables" in body or body.get("status") == "error"


@pytest.mark.asyncio
async def test_healthcheck_no_auth_required(monkeypatch, tmp_path):
    """GET /healthcheck is reachable without auth (liveness/readiness)."""
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "TOPOS_CONTROL_PLANE_URL": "",
            "TOPOS_DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/healthcheck")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
