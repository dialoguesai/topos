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


def _default_tier(field_transforms):
    bundle = RetrievalBundle(context_packet={"rows": [
        {"_table": "conversation_messages", "content": "hello test@example.com", "sent_at": "2026-03-04T10:11:12Z"},
    ]})
    return DisclosureFilterPipeline().apply(bundle, field_transforms=field_transforms, access_mode="raw",
                                            disclosure_tier="default_disclosure")


def test_the_default_tier_drops_the_ingest_transforms_and_keeps_the_others() -> None:
    """Below the owner's tier the PII transform was already applied when the row was stored, so it is not applied a
    second time; a transform that is not an ingest one (here the date coarsening) still is, and the pipeline says
    which of the two happened. (Until 1.5.0 only tests of the older person-to-person lane reached this.)"""
    filtered = _default_tier([
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction"]},
        {"table_id": "conversation_messages", "field": "sent_at", "transform_ids": ["timestamp_to_date"]},
    ])
    assert [name for name in filtered.filters_applied if name in ("nsfw_exclusion", "ingest_disclosure_pii",
                                                                  "field_transforms")] == [
        "nsfw_exclusion", "ingest_disclosure_pii", "field_transforms"]
    row = filtered.context_packet["rows"][0]
    assert row["sent_at"] == "2026-03-04"
    assert row["content"] == "hello test@example.com"          # stored already redacted in a real row; not re-run here


def test_a_transform_given_as_an_object_is_read_like_one_given_as_a_mapping() -> None:
    from types import SimpleNamespace

    as_object = SimpleNamespace(table_id="conversation_messages", field="sent_at", transform_id=None,
                                transform_ids=["timestamp_to_date"])
    filtered = _default_tier([as_object])
    assert filtered.context_packet["rows"][0]["sent_at"] == "2026-03-04"
    assert "ingest_disclosure_pii" in filtered.filters_applied and "field_transforms" in filtered.filters_applied


def test_only_ingest_transforms_at_the_default_tier_leave_nothing_to_apply() -> None:
    filtered = _default_tier([
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction"]}])
    assert "ingest_disclosure_pii" not in filtered.filters_applied
    assert "field_transforms" not in filtered.filters_applied and "nsfw_exclusion" in filtered.filters_applied
