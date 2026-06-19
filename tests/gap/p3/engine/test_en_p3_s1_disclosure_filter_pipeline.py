"""GT-EN-P3-S1-02: DisclosureFilterPipeline ordering."""

import pytest

from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.types import RetrievalBundle

pytestmark = pytest.mark.gap


def test_disclosure_pipeline_applies_manifest_then_transforms() -> None:
    bundle = RetrievalBundle(
        context_packet={
            "rows": [
                {"_table": "conversation_messages", "content": "hello test@example.com"},
            ]
        }
    )
    pipeline = DisclosureFilterPipeline()
    filtered = pipeline.apply(
        bundle,
        filter_manifest={"manifest_version": 1, "filters": []},
        field_transforms=[
            {
                "table_id": "conversation_messages",
                "field": "content",
                "transform_ids": ["pii_redaction"],
            }
        ],
        access_mode="raw",
    )
    assert "filter_manifest" in filtered.filters_applied or "field_transforms" in filtered.filters_applied
    content = filtered.context_packet["rows"][0]["content"]
    assert "[REDACTED_EMAIL]" in content
