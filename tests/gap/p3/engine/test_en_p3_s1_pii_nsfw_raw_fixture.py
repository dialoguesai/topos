"""GT-EN-P3-S1-02b: PII and NSFW on raw messenger fixture."""

import pytest

from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.types import RetrievalBundle

pytestmark = pytest.mark.gap


def test_messenger_raw_fixture_redacts_pii_and_nsfw() -> None:
    pipeline = DisclosureFilterPipeline()
    phone_row = pipeline.apply(
        RetrievalBundle(
            context_packet={
                "rows": [{"_table": "conversation_messages", "content": "Call +1-555-123-4567"}]
            }
        ),
        field_transforms=[
            {
                "table_id": "conversation_messages",
                "field": "content",
                "transform_ids": ["pii_redaction"],
            }
        ],
        access_mode="raw",
    )
    assert "[REDACTED_PHONE]" in phone_row.context_packet["rows"][0]["content"]

    nsfw_row = pipeline.apply(
        RetrievalBundle(
            context_packet={
                "rows": [{"_table": "conversation_messages", "content": "message with nsfw term"}]
            }
        ),
        field_transforms=[
            {
                "table_id": "conversation_messages",
                "field": "content",
                "transform_ids": ["nsfw_sanitization"],
            }
        ],
        access_mode="raw",
    )
    assert "nsfw" not in nsfw_row.context_packet["rows"][0]["content"].lower()
