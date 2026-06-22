"""NSFW classifier batch tests."""

from unittest.mock import patch

from topos.sanitization.nsfw_classifier import classify_nsfw_batch, classify_nsfw_text


def test_heuristic_nsfw_detects_token():
    with patch("topos.sanitization.nsfw_classifier.nsfw_classifier_available", return_value=False):
        is_nsfw, score, label = classify_nsfw_text("this message is nsfw material")
    assert is_nsfw is True
    assert score >= 0.5


def test_heuristic_safe_text():
    with patch("topos.sanitization.nsfw_classifier.nsfw_classifier_available", return_value=False):
        is_nsfw, score, label = classify_nsfw_text("let us meet for coffee tomorrow")
    assert is_nsfw is False


def test_classify_nsfw_batch():
    result = classify_nsfw_batch(
        [
            {"id": "1", "text": "normal planning"},
            {"id": "2", "text": "explicit xxx content"},
        ]
    )
    assert result["status"] in ("ok", "disabled")
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["1"]["nsfw"] is False
    assert by_id["2"]["nsfw"] is True
