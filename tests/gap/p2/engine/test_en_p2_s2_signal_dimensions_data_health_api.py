"""
Gap: Dimensions/health — missing → profile + health endpoints
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_dimensions_and_data_health_api(monkeypatch) -> None:
    from topos.app import app
    from topos.uds import UDSChannelApp

    class FakeService:
        def list_dimensions(self):
            return {"dimensions": [{"id": "memory", "coverage_score": 0.5}]}

        def get_data_health(self, **kw):
            return {"dimensions": [], "provider_status": {"ollama": "up", "huggingface": "up"}}

    monkeypatch.setattr("topos.api.signal.get_signal_service", lambda: FakeService())
    # The signal router answers only the owner, so reach it the way the owner's
    # app does, over the socket transport, instead of overriding its auth.
    transport = ASGITransport(app=UDSChannelApp(app))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        d = await client.get("/v1/signal/dimensions")
        h = await client.get("/v1/signal/data-health")
    assert d.status_code == 200
    assert h.status_code == 200
