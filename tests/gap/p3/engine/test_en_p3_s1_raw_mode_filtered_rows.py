"""GT-EN-P3-S1-01c: Raw mode row caps and field transforms."""

import pytest

from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import make_adapter_bundle, messages_manifest

pytestmark = pytest.mark.gap


def test_raw_retrieval_respects_row_cap_and_pii_redaction() -> None:
    bundle = make_adapter_bundle()
    for i in range(105):
        bundle.canonical.upsert(
            "conversation_messages",
            {"record_id": f"extra-{i}", "content": f"row {i} test@example.com"},
        )
    adapter = DefaultSignalRetrievalAdapter(bundle)
    retrieval = adapter.retrieve(
        RetrievalRequest(manifest=messages_manifest(), access_mode="raw")
    )
    rows = retrieval.context_packet.get("rows") or []
    assert len(rows) <= 100

    pipeline = DisclosureFilterPipeline()
    filtered = pipeline.apply(
        retrieval,
        field_transforms=[
            {
                "table_id": "conversation_messages",
                "field": "content",
                "transform_ids": ["pii_redaction"],
            }
        ],
        access_mode="raw",
    )
    content = (filtered.context_packet.get("rows") or [{}])[0].get("content", "")
    assert "test@example.com" not in content
    assert "[REDACTED_EMAIL]" in content
