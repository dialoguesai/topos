"""
Gap: Data Health — absent → coverage/freshness scores computed
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.features.signal.data_health import DataHealthComputer
from topos.storage.adapters.fakes import (
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)
from topos.storage.adapters.factory import AdapterBundle

pytestmark = pytest.mark.gap


def test_data_health_computer_returns_all_ten_dimensions() -> None:
    bundle = AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
        query_session=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
        backend="memory",
    )
    profiles = DataHealthComputer(bundle).compute(deferred_jobs=["topics"])
    assert set(profiles.keys()) == {
        "profile",
        "time",
        "interests",
        "relationships",
        "work",
        "memory",
        "wellbeing",
        "resources",
        "places",
        "intentions",
    }
    assert "coverage_score" in profiles["memory"]
    assert "score" in profiles["memory"]
    assert profiles["memory"]["measured"] is False  # empty stores → unmeasured
    assert "ollama_unreachable" in profiles["memory"]["provider_failures"]
