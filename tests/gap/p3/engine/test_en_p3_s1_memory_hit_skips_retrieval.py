"""GT-EN-P3-S1-01g: skip_retrieval returns empty stores_touched."""

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import make_adapter_bundle, messages_manifest

pytestmark = pytest.mark.gap


def test_skip_retrieval_returns_empty_bundle() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(
            manifest=messages_manifest(),
            access_mode="raw",
            skip_retrieval=True,
        )
    )
    assert result.stores_touched == []
    assert result.context_packet == {}
