"""Topic cluster member API returns 404 for unknown cluster."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_topic_cluster_members_unknown_id_returns_404() -> None:
    from topos.app import app
    from topos.uds import UDSChannelApp

    # The signal router answers only the owner, so reach it the way the owner's
    # app does, over the socket transport, instead of overriding its auth.
    transport = ASGITransport(app=UDSChannelApp(app))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/v1/signal/topic-clusters/00000000-0000-0000-0000-000000000000/members",
        )

    assert resp.status_code == 404
