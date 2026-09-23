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
    from topos.principal import OWNER_APP, Principal

    monkeypatch.setattr(
        "topos.features.signal.service.get_signal_service",
        lambda conn=None: type(
            "S",
            (),
            {"list_vectors": lambda self, **kw: {"items": [], "total": 0, "offset": 0, "limit": 50}},
        )(),
    )
    # Every signal_* type is the owner's; the dispatcher refuses it without an
    # owner principal (tests/topos/test_signal_intelligence_handlers.py).
    resp = await handle_control_plane_request(
        {"id": "req-1", "type": "signal_list_vectors", "payload": {"limit": 10}},
        principal=Principal(cls=OWNER_APP, channel="uds"),
    )
    assert resp["status"] == "ok"
    assert "items" in resp["payload"]
