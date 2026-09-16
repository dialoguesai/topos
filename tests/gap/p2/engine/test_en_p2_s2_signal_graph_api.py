"""
Gap: Graph API — missing → nodes/edges JSON
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_signal_graph_api(monkeypatch) -> None:
    from topos.app import app
    from topos.uds import UDSChannelApp

    monkeypatch.setattr(
        "topos.api.signal.get_signal_service",
        lambda: type(
            "S",
            (),
            {"list_graph": lambda self, **kw: {"nodes": [{"node_id": "n1"}], "edges": []}},
        )(),
    )
    # The signal router answers only the owner, so reach it the way the owner's
    # app does, over the socket transport, instead of overriding its auth.
    transport = ASGITransport(app=UDSChannelApp(app))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/signal/graph")
    assert resp.status_code == 200
    assert "nodes" in resp.json()
