"""Privacy-filter PII redaction (mocked transformers pipeline)."""

from unittest.mock import MagicMock, patch

import pytest

from topos.sanitization.privacy_filter import (
    PRIVACY_FILTER_TRANSFORM_IDS,
    apply_text_transform_with_privacy_filter,
)


def test_privacy_filter_transform_ids_cover_pii_family():
    assert "pii_redaction" in PRIVACY_FILTER_TRANSFORM_IDS
    assert "name_removal" in PRIVACY_FILTER_TRANSFORM_IDS
    assert "contact_removal" in PRIVACY_FILTER_TRANSFORM_IDS


def test_apply_pii_redaction_replaces_detected_spans():
    text = "My name is Alice Smith and email alice@example.com"
    fake_entities = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice Smith", "start": 11, "end": 22},
        {"entity_group": "private_email", "score": 0.99, "word": "alice@example.com", "start": 33, "end": 50},
    ]
    fake_pipe = MagicMock(return_value=fake_entities)

    with patch("topos.sanitization.privacy_filter._get_pipeline", return_value=fake_pipe):
        out = apply_text_transform_with_privacy_filter(
            text,
            "pii_redaction",
            {},
        )

    assert out == "My name is [NAME] and email [EMAIL]"
    fake_pipe.assert_called_once()
    assert fake_pipe.call_args[0][0] == text
    assert fake_pipe.call_args[1]["aggregation_strategy"] == "simple"


def test_name_removal_only_redacts_people():
    text = "My name is Alice Smith and email alice@example.com"
    fake_entities = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice Smith", "start": 11, "end": 22},
        {"entity_group": "private_email", "score": 0.99, "word": "alice@example.com", "start": 33, "end": 50},
    ]
    fake_pipe = MagicMock(return_value=fake_entities)

    with patch("topos.sanitization.privacy_filter._get_pipeline", return_value=fake_pipe):
        out = apply_text_transform_with_privacy_filter(
            text,
            "name_removal",
            {},
        )

    assert out == "My name is [NAME] and email alice@example.com"


def test_contact_removal_only_redacts_contact_details():
    text = "My name is Alice Smith and email alice@example.com"
    fake_entities = [
        {"entity_group": "private_person", "score": 0.99, "word": "Alice Smith", "start": 11, "end": 22},
        {"entity_group": "private_email", "score": 0.99, "word": "alice@example.com", "start": 33, "end": 50},
    ]
    fake_pipe = MagicMock(return_value=fake_entities)

    with patch("topos.sanitization.privacy_filter._get_pipeline", return_value=fake_pipe):
        out = apply_text_transform_with_privacy_filter(
            text,
            "contact_removal",
            {},
        )

    assert out == "My name is Alice Smith and email [EMAIL]"


def test_unknown_transform_raises():
    with pytest.raises(ValueError, match="not handled by privacy-filter"):
        apply_text_transform_with_privacy_filter("hello", "nsfw_sanitization", {})
