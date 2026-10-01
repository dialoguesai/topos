"""NSFW classifier: the strict cutoff on the NSFW label, the unchanged rules around it, and the batch shape."""

import math
from unittest.mock import patch

import pytest

from topos.sanitization import nsfw_classifier
from topos.sanitization.nsfw_classifier import (
    DEFAULT_NSFW_CLASSIFIER_THRESHOLD,
    HEURISTIC_NSFW_SCORE,
    classify_nsfw_batch,
    classify_nsfw_text,
    is_nsfw_result,
    nsfw_threshold,
)


def _pipeline(label: str, score: float):
    """A stand-in for the transformers pipeline (top_k=None): every label, best first."""
    other = "SFW" if label.upper() == "NSFW" else "NSFW"

    def pipe(_text):
        return [[{"label": label, "score": score}, {"label": other, "score": 1.0 - score}]]

    return lambda _model: pipe


def _classify(label: str, score: float, **kwargs):
    with patch.object(nsfw_classifier, "nsfw_classifier_available", return_value=True), \
            patch.object(nsfw_classifier, "_get_pipeline", _pipeline(label, score)):
        return classify_nsfw_text("a short time-log line", **kwargs)


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
    """The batch shape, decided by a stand-in model so the result does not depend on a Hub cache."""
    scores = {"normal planning": ("SFW", 0.98), "explicit xxx content": ("NSFW", 0.97)}

    def pipe(text):
        label, score = scores[text]
        return [[{"label": label, "score": score}]]

    with patch.object(nsfw_classifier, "nsfw_classifier_available", return_value=True), \
            patch.object(nsfw_classifier, "_get_pipeline", lambda _model: pipe):
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
    assert by_id["2"]["score"] == 0.97


# --- the gate ---------------------------------------------------------------------------------------------------


def test_the_default_cutoff_is_091_and_the_setting_carries_it():
    from topos.config.settings import Settings

    assert DEFAULT_NSFW_CLASSIFIER_THRESHOLD == 0.91
    assert Settings.model_fields["nsfw_classifier_threshold"].default == DEFAULT_NSFW_CLASSIFIER_THRESHOLD


@pytest.mark.parametrize(
    "score, flagged",
    [
        (0.502, False),       # the coin-flip that used to hard-withhold an entry
        (0.9, False),
        (0.91, False),        # AT the cutoff is not above it
        (0.9100001, True),
        (0.95, True),
        (0.969, True),
    ],
)
def test_an_nsfw_label_counts_only_strictly_above_the_cutoff(score, flagged):
    is_nsfw, stored, label = _classify("NSFW", score)
    assert is_nsfw is flagged
    # The stored score is still the classifier's confidence in its top label, flagged or not.
    assert stored == score and label == "nsfw"


@pytest.mark.parametrize("label", ["nsfw", "LABEL_1", "toxic", "obscene", "very_nsfw"])
def test_every_nsfw_spelling_is_gated_the_same_way(label):
    assert is_nsfw_result(label, 0.91, 0.91) is False
    assert is_nsfw_result(label, 0.92, 0.91) is True


@pytest.mark.parametrize("label", ["SFW", "label_0", "neutral", "safe"])
def test_a_safe_label_never_counts_at_any_score(label):
    is_nsfw, _score, _label = _classify(label, 0.999)
    assert is_nsfw is False
    assert is_nsfw_result(label, 1.0, 0.0) is False


def test_an_unknown_label_keeps_its_old_rule_at_or_above_max_cutoff_and_floor():
    # max(0.91, 0.85) = 0.91, inclusive, as it always was for a label the classifier does not name.
    assert is_nsfw_result("spicy", 0.909, 0.91) is False
    assert is_nsfw_result("spicy", 0.91, 0.91) is True
    # Below the floor the cutoff alone is not enough.
    assert is_nsfw_result("spicy", 0.849, 0.5) is False
    assert is_nsfw_result("spicy", 0.85, 0.5) is True
    assert is_nsfw_result("", 0.0, 0.0) is False


def test_an_nsfw_label_with_a_broken_score_fails_closed():
    assert is_nsfw_result("nsfw", math.nan, 0.91) is True
    assert is_nsfw_result("sfw", math.nan, 0.91) is False
    assert is_nsfw_result("spicy", math.nan, 0.5) is False


def test_the_configured_cutoff_is_the_one_applied(monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", 0.6)
    assert _classify("NSFW", 0.6)[0] is False
    assert _classify("NSFW", 0.61)[0] is True
    # An explicit cutoff wins over the setting.
    assert _classify("NSFW", 0.61, threshold=0.7)[0] is False


@pytest.mark.parametrize("value", [None, "high", True, math.nan, math.inf, -0.1, 1.0, 1.5])
def test_a_cutoff_outside_0_to_1_reads_as_the_default(monkeypatch, value):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", value)
    assert nsfw_threshold() == DEFAULT_NSFW_CLASSIFIER_THRESHOLD
    assert nsfw_threshold(value) == DEFAULT_NSFW_CLASSIFIER_THRESHOLD


@pytest.mark.parametrize("value", [0.0, 0.5, 0.91, 0.999])
def test_a_cutoff_inside_0_to_1_is_used_as_given(value):
    assert nsfw_threshold(value) == value


def test_the_heuristic_is_not_gated_by_the_cutoff(monkeypatch):
    import topos.config.settings as config

    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", 0.99)
    with patch.object(nsfw_classifier, "nsfw_classifier_available", return_value=False):
        assert classify_nsfw_text("this message is nsfw material") == (True, HEURISTIC_NSFW_SCORE, "heuristic")


def test_a_pipeline_error_falls_back_to_the_heuristic_ungated():
    def broken(_model):
        raise RuntimeError("model unavailable")

    with patch.object(nsfw_classifier, "nsfw_classifier_available", return_value=True), \
            patch.object(nsfw_classifier, "_get_pipeline", broken):
        assert classify_nsfw_text("xxx", threshold=0.99) == (True, HEURISTIC_NSFW_SCORE, "heuristic_fallback")
        assert classify_nsfw_text("coffee", threshold=0.0)[0] is False


def test_the_heuristic_score_cannot_be_a_classifier_output():
    """The re-check tells heuristic rows apart by this exact double; a float32 softmax output never equals it."""
    import struct

    as_float32 = struct.unpack("f", struct.pack("f", HEURISTIC_NSFW_SCORE))[0]
    assert as_float32 != HEURISTIC_NSFW_SCORE
