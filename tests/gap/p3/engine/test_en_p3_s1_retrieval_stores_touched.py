"""GT-EN-P3-S1-01b: mode-aware store selection."""

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import availability_manifest, make_adapter_bundle, messages_manifest, relationship_manifest

pytestmark = pytest.mark.gap


def test_raw_mode_touches_canonical() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(manifest=messages_manifest(), access_mode="raw")
    )
    assert "canonical" in result.stores_touched


def test_summary_mode_touches_signal_only() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(manifest=relationship_manifest(), access_mode="summary")
    )
    assert "signal" in result.stores_touched
    assert "canonical" not in result.stores_touched
