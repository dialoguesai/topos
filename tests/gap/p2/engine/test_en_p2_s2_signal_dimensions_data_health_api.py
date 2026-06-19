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
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    class FakeService:
        def list_dimensions(self):
            return {"dimensions": [{"id": "memory", "coverage_score": 0.5}]}

        def get_data_health(self, **kw):
            return {"dimensions": [], "provider_status": {"ollama": "up", "huggingface": "up"}}

    app.dependency_overrides[require_api_key] = _fake_key
    monkeypatch.setattr("topos.api.signal.get_signal_service", lambda: FakeService())
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            d = await client.get("/v1/signal/dimensions", headers={"Authorization": "Bearer test-key"})
            h = await client.get("/v1/signal/data-health", headers={"Authorization": "Bearer test-key"})
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert d.status_code == 200
    assert h.status_code == 200
