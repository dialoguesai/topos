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


def test_resolve_disclosure_tier_grantee_cannot_forge_owner_raw():
    """B.2: a grantee request must never be elevated to owner_raw via an explicit tier."""
    # Explicit owner_raw on a grantee request is clamped to default_disclosure.
    assert (
        resolve_disclosure_tier(is_grantee_request=True, explicit_tier="owner_raw")
        == "default_disclosure"
    )
    # Same when grantee status is inferred from a non-owner requester_id.
    assert (
        resolve_disclosure_tier(
            requester_id="grantee-123", owner_id="owner-9", explicit_tier="owner_raw"
        )
        == "default_disclosure"
    )
    # A disclosure_ceiling above default must still fail closed for a grantee.
    assert (
        resolve_disclosure_tier(is_grantee_request=True, disclosure_ceiling="raw")
        == "default_disclosure"
    )


def test_resolve_disclosure_tier_owner_explicit_tier_still_honored():
    """Non-grantee (owner) callers may still set an explicit tier in either direction."""
    assert resolve_disclosure_tier(requester_id="owner", explicit_tier="owner_raw") == "owner_raw"
    assert (
        resolve_disclosure_tier(requester_id="owner", explicit_tier="default_disclosure")
        == "default_disclosure"
    )


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
