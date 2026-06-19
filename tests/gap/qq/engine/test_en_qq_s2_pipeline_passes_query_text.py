"""GT-EN-QQ-S2-01: Pipeline passes query_text into retrieval."""

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from qq_helpers import ai_conversations_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap


def test_retrieval_request_query_text_changes_summary_packet() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    manifest = ai_conversations_manifest()
    with_query = adapter.retrieve(
        RetrievalRequest(manifest=manifest, access_mode="summary", query_text="docker nginx")
    )
    without_query = adapter.retrieve(
        RetrievalRequest(manifest=manifest, access_mode="summary", query_text="")
    )
    assert with_query.retrieval_metadata.get("retrieval_strategy") in {"query_aware", "dimension_dump"}
    assert with_query.context_packet.get("access_mode") == "summary"
    assert without_query.context_packet.get("summaries") is not None
