"""Minimal deployment smoke: health + auth so deployments stay stable."""

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
async def test_smoke_health_and_auth(monkeypatch, tmp_path):
    """Smoke: health 200; /api/local 401 without auth, 200 with valid Bearer."""
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "CONTROL_PLANE_URL": "",
            "TOPOS_DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = await client.get("/healthcheck")
            assert health.status_code == 200, "healthcheck must return 200"
            assert health.json().get("status") == "ok"

            no_auth = await client.post("/api/local/list_database_tables")
            assert no_auth.status_code == 401, "list_database_tables without auth must return 401"

            with_auth = await client.post(
                "/api/local/list_database_tables",
                headers={"Authorization": "Bearer test-key"},
            )
            assert with_auth.status_code == 200, "list_database_tables with valid key must return 200"
            body = with_auth.json()
            assert "tables" in body or body.get("status") == "error"
