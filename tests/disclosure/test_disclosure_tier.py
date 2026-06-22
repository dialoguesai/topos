"""Tests for disclosure tier read path."""

from topos.disclosure.tier import (
    apply_disclosure_tier_to_rows,
    resolve_disclosure_tier,
    strip_ingest_pii_transforms,
)


def test_resolve_disclosure_tier_owner_vs_grantee():
    assert resolve_disclosure_tier(requester_id="owner") == "owner_raw"
    assert resolve_disclosure_tier(requester_id="grantee-123") == "default_disclosure"
    assert resolve_disclosure_tier(is_grantee_request=True) == "default_disclosure"


def test_apply_disclosure_tier_swaps_content():
    rows = [
        {
            "record_id": "m1",
            "content": "Email me at secret@example.com",
            "content_disclosure": "Email me at [EMAIL]",
        }
    ]
    out = apply_disclosure_tier_to_rows(
        rows,
        table="conversation_messages",
        tier="default_disclosure",
    )
    assert out[0]["content"] == "Email me at [EMAIL]"
    assert "content_disclosure" not in out[0]


def test_apply_disclosure_tier_owner_keeps_raw():
    rows = [{"record_id": "m1", "content": "raw", "content_disclosure": "redacted"}]
    out = apply_disclosure_tier_to_rows(rows, table="conversation_messages", tier="owner_raw")
    assert out[0]["content"] == "raw"


def test_strip_ingest_pii_transforms():
    transforms = [
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
    ]
    stripped = strip_ingest_pii_transforms(transforms)
    assert stripped is None
    transforms_mixed = [
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction", "timestamp_to_date"]},
    ]
    stripped_mixed = strip_ingest_pii_transforms(transforms_mixed)
    assert stripped_mixed == [
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["timestamp_to_date"]},
    ]
