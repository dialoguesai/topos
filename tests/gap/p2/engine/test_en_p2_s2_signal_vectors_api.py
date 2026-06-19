"""
Gap: Vectors API — 404/missing → paginated metadata list
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_signal_vectors_api(monkeypatch) -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    monkeypatch.setattr(
        "topos.api.signal.get_signal_service",
        lambda: type(
            "S",
            (),
            {
                "list_vectors": lambda self, **kw: {"items": [{"embedding_id": "e1", "dims": 384}], "total": 1, "offset": 0, "limit": 50}
            },
        )(),
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/signal/vectors", headers={"Authorization": "Bearer test-key"})
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert "vector" not in body["items"][0]
