import os

import pytest
from topos.testing.lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_analytics_profile_returns_results():
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/analytics", params={"profile_id": "chatgpt_dev"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["profile_id"] == "chatgpt_dev"
    assert "results" in payload
