"""GT-EN-P3-S1-01d: Summary mode excludes raw message bodies."""

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import make_adapter_bundle, relationship_manifest

pytestmark = pytest.mark.gap


def test_summary_retrieval_has_no_content_field() -> None:
    bundle = make_adapter_bundle()
    bundle.signal.put_summary(
        {
            "dimension": "relationship",
            "summary_text": "Summary only",
            "content": "raw body must not leak",
        }
    )
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(manifest=relationship_manifest(), access_mode="summary")
    )
    summaries = result.context_packet.get("summaries") or []
    assert summaries
    for item in summaries:
        assert "content" not in item
