"""Topos app healthcheck (migrated from tests/engine)."""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from topos.testing.lifespan import LifespanManager

os.environ.setdefault("TOPOS_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CONTROL_PLANE_URL", "")

from topos.app import app  # noqa: E402


@pytest.mark.asyncio
async def test_healthcheck_ok():
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/healthcheck")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"
    assert "time" in body
    assert "control_plane_connection" in body
    assert "sync_connection" in body


@pytest.mark.asyncio
async def test_healthcheck_reports_a_database_verdict():
    """`status: ok` only ever meant "the event loop answered".

    A node whose database is unreadable answers this route perfectly, so the
    liveness verdict and the data verdict have to be separate fields.
    """
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/healthcheck")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"
    assert "db_ok" in body
    # None (no database configured) or True (readable) — never a false accusation
    # against a node that answered.
    assert body["db_ok"] in (None, True)
