"""
Gap: Signal APIs — open list → owner auth required
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

pytestmark = pytest.mark.gap


def test_signal_service_clamps_list_limit() -> None:
    from topos.features.signal.service import SignalService

    from helpers import make_adapter_bundle

    svc = SignalService(make_adapter_bundle())
    out = svc.list_vectors(limit=9999)
    assert out["limit"] <= 500
