import importlib
import os
import sys

import pytest
from topos.testing.lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_app_registry_stubs():
    os.environ["TOPOS_KEY"] = "test-key"
    if "topos.app" in sys.modules:
        importlib.reload(sys.modules["topos.app"])
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/apps", headers={"Authorization": "Bearer test-key"})
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["status"] == "stub"

            resp = await client.get(
                "/apps/app_123/sources", headers={"Authorization": "Bearer test-key"}
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["status"] == "stub"
