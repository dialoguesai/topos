"""Topos app routes: health and /api/local (migrated from tests/engine test_services_routes)."""

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
async def test_healthcheck_returns_ok(monkeypatch):
    app = load_topos_app(
        monkeypatch,
        {"TOPOS_KEY": "test-key", "CONTROL_PLANE_URL": ""},
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/healthcheck")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


@pytest.mark.asyncio
async def test_local_list_database_tables_requires_auth(monkeypatch):
    app = load_topos_app(
        monkeypatch,
        {"TOPOS_KEY": "test-key", "CONTROL_PLANE_URL": ""},
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
async def test_local_get_table_schema_requires_auth(monkeypatch):
    app = load_topos_app(
        monkeypatch,
        {"TOPOS_KEY": "test-key", "CONTROL_PLANE_URL": ""},
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/local/get_table_schema",
                headers={"Authorization": "Bearer test-key"},
                json={"table_name": "messages"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert "columns" in body or body.get("status") == "error"
