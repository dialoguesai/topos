"""The NSFW tagging facade: the rule decides, nothing is loaded, the batch and HTTP shapes still answer."""

from __future__ import annotations

import inspect

from topos.sanitization import nsfw_classifier
from topos.sanitization.explicit_wording import RULE_ID
from topos.sanitization.nsfw_classifier import (
    NSFW_CLASSIFY_MAX_BATCH,
    NSFW_TAGGER_ID,
    TIER_SCORES,
    classify_nsfw_batch,
    classify_nsfw_text,
)


def test_the_tagger_is_the_rule_and_names_itself():
    assert NSFW_TAGGER_ID == RULE_ID == "explicit-wording/v1"
    assert classify_nsfw_text("they were sexting all night") == (True, 1.0, "unambiguous")
    assert classify_nsfw_text("we had sex") == (True, 0.5, "phrase")
    assert classify_nsfw_text("turned on the lights") == (False, 0.0, "none")


def test_empty_text_and_a_disabled_tagger_answer_without_a_verdict(monkeypatch):
    import topos.config.settings as config

    assert classify_nsfw_text("") == (False, 0.0, "empty")
    assert classify_nsfw_text("   ") == (False, 0.0, "empty")
    monkeypatch.setattr(config.settings, "nsfw_classifier_enabled", False)
    assert classify_nsfw_text("they were sexting all night") == (False, 0.0, "disabled")
    result = classify_nsfw_batch([{"id": "1", "text": "they were sexting all night"}])
    assert result["status"] == "disabled" and result["items"][0]["nsfw"] is False
    assert result["model"] == RULE_ID


def test_classify_nsfw_batch():
    result = classify_nsfw_batch(
        [
            {"id": "1", "text": "normal planning"},
            {"id": "2", "text": "a pornographic film"},
            {"id": "3", "text": ""},
        ]
    )
    assert result["status"] == "ok" and result["model"] == RULE_ID and result["provider"] == "rule"
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["1"] == {"id": "1", "nsfw": False, "score": 0.0, "label": "none"}
    assert by_id["2"] == {"id": "2", "nsfw": True, "score": 1.0, "label": "unambiguous"}
    assert by_id["3"]["label"] == "empty"


def test_a_batch_over_the_limit_is_refused_whole():
    result = classify_nsfw_batch([{"id": str(i), "text": "x"} for i in range(NSFW_CLASSIFY_MAX_BATCH + 1)])
    assert result["status"] == "too_large" and result["items"] == []


def test_the_scores_are_the_tiers():
    assert TIER_SCORES == {"unambiguous": 1.0, "phrase": 0.5, None: 0.0}


def test_no_model_is_imported_loaded_or_prewarmed():
    """The retired classifier must not come back through this module: no transformers, no torch, no Hub, no cache
    slot, no prewarm hook. The one dependency is the rule."""
    src = inspect.getsource(nsfw_classifier)
    for forbidden in ("transformers", "torch", "pipeline(", "huggingface", "model_cache", "ModelSlot",
                      "prewarm", "hub_pipeline", "michellejieli"):
        assert forbidden not in src.replace("``michellejieli/NSFW_text_classifier``", ""), forbidden
    from topos.engine.model_cache import _SUBTYPE_TO_SLOT, ModelSlot

    assert "content_nsfw_classification" not in _SUBTYPE_TO_SLOT
    assert not any(slot.value == "nsfw" for slot in ModelSlot)
    from topos.sanitization import prewarm

    assert prewarm._state["models_total"] == 1 and not hasattr(prewarm, "NSFW_REPO")
