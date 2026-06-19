"""
Gap: Capabilities — no tiers → tier.vector/graph in register payload
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.engine.registration import build_engine_capabilities

pytestmark = pytest.mark.gap


def test_capability_tiers_present() -> None:
    caps = build_engine_capabilities()
    assert caps["schema_version"] == "v2"
    assert "tier.vector" in caps["capability_tiers"]
    assert "tier.graph" in caps["capability_tiers"]
    assert "huggingface" in caps["signal_providers"]
