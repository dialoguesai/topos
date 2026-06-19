"""
Gap: WS — no handlers → signal list via WS message types
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_signal_ws_handlers(monkeypatch) -> None:
    from topos.core.handlers import handle_control_plane_request

    monkeypatch.setattr(
        "topos.features.signal.service.get_signal_service",
        lambda conn=None: type(
            "S",
            (),
            {"list_vectors": lambda self, **kw: {"items": [], "total": 0, "offset": 0, "limit": 50}},
        )(),
    )
    resp = await handle_control_plane_request(
        {"id": "req-1", "type": "signal_list_vectors", "payload": {"limit": 10}}
    )
    assert resp["status"] == "ok"
    assert "items" in resp["payload"]
