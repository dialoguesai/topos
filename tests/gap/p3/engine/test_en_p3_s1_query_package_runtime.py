"""GT-EN-P3-S1-01: query package runtime imports."""

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter, SignalRetrievalAdapter
from topos.query.types import RetrievalRequest

pytestmark = pytest.mark.gap


def test_signal_retrieval_adapter_import_and_retrieve_signature() -> None:
    assert SignalRetrievalAdapter is DefaultSignalRetrievalAdapter
    assert hasattr(DefaultSignalRetrievalAdapter, "retrieve")
    assert "request" in DefaultSignalRetrievalAdapter.retrieve.__code__.co_varnames
