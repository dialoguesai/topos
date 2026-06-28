"""Topic cluster member API returns 404 for unknown cluster."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_topic_cluster_members_unknown_id_returns_404() -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/signal/topic-clusters/00000000-0000-0000-0000-000000000000/members",
                headers={"Authorization": "Bearer test-key"},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert resp.status_code == 404
