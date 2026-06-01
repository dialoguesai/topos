"""Stage 11: contact_display_names and message_contact_participation catalog validation."""

import pytest
from shared.filtering import FilterInstance, FilterManifest, merge_filter_manifests, validate_filter_params


def test_contact_display_names_params():
    validate_filter_params("contact_display_names", {"enabled": True})
    validate_filter_params("contact_display_names", {"enabled": False})
    with pytest.raises(ValueError):
        validate_filter_params("contact_display_names", {"enabled": "yes"})


def test_message_contact_participation_params():
    validate_filter_params(
        "message_contact_participation",
        {"mode": "all", "contact_ids": [], "match": "sender_only"},
    )
    with pytest.raises(ValueError):
        validate_filter_params(
            "message_contact_participation",
            {"mode": "invalid", "contact_ids": [], "match": "sender_only"},
        )


def test_merge_contact_display_names_stricter_false_wins():
    m1 = FilterManifest(
        filters=[FilterInstance(filter_id="contact_display_names", params={"enabled": True})]
    )
    m2 = FilterManifest(
        filters=[FilterInstance(filter_id="contact_display_names", params={"enabled": False})]
    )
    merged = merge_filter_manifests([m1, m2])
    f = merged.get_filter("contact_display_names")
    assert f is not None
    assert f.params.get("enabled") is False
