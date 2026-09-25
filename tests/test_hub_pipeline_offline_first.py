"""A cached model loads with no Hub request; only a cache miss talks to the Hub.

The boot of 2026-09-25 sat for hours in a Hub GET that carries no timeout
(transformers' tokenizer loader listing chat templates), with the tray on
"preparing models (0 of 2)" and every later caller of the privacy-filter slot
waiting behind it. These tests pin the shape that avoids the request entirely:
tokenizer and model built with `local_files_only=True` first, handed to
`pipeline()` as objects, and the Hub consulted only after that attempt fails
the way transformers fails a cache miss. They drive a fake `transformers` so no
weights are needed; the real loads were measured at zero requests.
"""

from __future__ import annotations

import logging
import os
import sys
import types

import pytest

from topos.sanitization import hub_pipeline

REPO = "example/some-classifier"
CACHE_MISS = OSError(
    "We couldn't connect to 'https://huggingface.co' to load the files, "
    "and couldn't find them in the cached files."
)


class Calls:
    def __init__(self) -> None:
        self.from_pretrained: list[tuple[str, str, dict]] = []
        self.pipeline: list[tuple[str, object, dict]] = []


def fake_transformers(
    calls: Calls, *, cached: bool = True, model_error: BaseException | None = None
):
    """The four names load_pipeline touches, recording every call.

    `cached=False` is an empty cache: every `local_files_only` load raises the
    OSError transformers raises for one. `model_error` is raised by the model
    head's loader on top of that.
    """
    mod = types.ModuleType("transformers")

    def _auto(name: str, error: BaseException | None = None):
        class Auto:
            @classmethod
            def from_pretrained(cls, model_id: str, **kwargs):
                calls.from_pretrained.append((name, model_id, kwargs))
                if not cached and kwargs.get("local_files_only"):
                    raise CACHE_MISS
                if error is not None:
                    raise error
                return (name, model_id)

        Auto.__name__ = name
        return Auto

    mod.AutoTokenizer = _auto("AutoTokenizer")
    mod.AutoModelForTokenClassification = _auto("AutoModelForTokenClassification", model_error)
    mod.AutoModelForSequenceClassification = _auto("AutoModelForSequenceClassification", model_error)

    def pipeline(task: str, model, **kwargs):
        calls.pipeline.append((task, model, kwargs))
        return ("pipeline", task, model, kwargs)

    mod.pipeline = pipeline
    return mod


@pytest.fixture
def calls(monkeypatch):
    calls = Calls()
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers(calls))
    return calls


def _load(model_class: str = "AutoModelForTokenClassification", **kwargs):
    return hub_pipeline.load_pipeline(
        "token-classification", REPO, model_class=model_class, **kwargs
    )


def test_a_cached_model_loads_both_parts_offline_and_hands_pipeline_objects(calls):
    handle = _load(device="cpu")

    assert calls.from_pretrained == [
        ("AutoTokenizer", REPO, {"local_files_only": True}),
        ("AutoModelForTokenClassification", REPO, {"local_files_only": True}),
    ]
    # pipeline() gets objects, so it has nothing left to resolve against the Hub.
    assert calls.pipeline == [
        (
            "token-classification",
            ("AutoModelForTokenClassification", REPO),
            {"tokenizer": ("AutoTokenizer", REPO), "device": "cpu"},
        )
    ]
    assert handle[0] == "pipeline"


def test_a_cold_cache_fails_the_offline_attempt_and_loads_through_the_hub(
    monkeypatch, caplog
):
    calls = Calls()
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers(calls, cached=False))

    with caplog.at_level(logging.INFO, logger="topos.sanitization.hub_pipeline"):
        _load(device="cpu")

    # The tokenizer is the first thing asked for, and the first to refuse.
    assert calls.from_pretrained == [("AutoTokenizer", REPO, {"local_files_only": True})]
    assert calls.pipeline == [("token-classification", REPO, {"device": "cpu"})]
    assert "loading from the Hub" in caplog.text


def test_a_file_the_loader_wants_but_the_cache_lacks_is_a_cache_miss_too(monkeypatch):
    """A newer transformers may ask for a file an older download never fetched."""
    calls = Calls()
    monkeypatch.setitem(
        sys.modules, "transformers", fake_transformers(calls, model_error=CACHE_MISS)
    )

    _load()

    assert [name for name, _repo, _kw in calls.from_pretrained] == [
        "AutoTokenizer",
        "AutoModelForTokenClassification",
    ]
    assert calls.pipeline == [("token-classification", REPO, {})]


def test_a_failure_that_is_not_a_cache_miss_propagates_without_a_hub_retry(monkeypatch):
    """A wedged torch load is not fixed by a download; the retry would reopen the hang."""
    calls = Calls()
    monkeypatch.setitem(
        sys.modules, "transformers", fake_transformers(calls, model_error=RuntimeError("mps wedged"))
    )

    with pytest.raises(RuntimeError, match="mps wedged"):
        _load()

    assert calls.pipeline == []


def test_an_unknown_model_class_fails_before_anything_loads(calls):
    with pytest.raises(AttributeError):
        _load(model_class="AutoModelForNoSuchHead")

    assert calls.from_pretrained == []
    assert calls.pipeline == []


class _PassThroughCache:
    """The slot cache without residency bookkeeping: run the loader, hand back its result."""

    def acquire(self, slot, model_id, loader):
        return loader(), False


def test_the_privacy_filter_loads_through_the_cache_first(calls, monkeypatch):
    from topos.engine import model_cache
    from topos.sanitization import privacy_filter

    repo = privacy_filter.PRIVACY_FILTER_MODEL_ID
    monkeypatch.setattr(privacy_filter, "_resolve_device", lambda: "cpu")
    monkeypatch.setattr(model_cache, "get_model_cache", lambda: _PassThroughCache())

    privacy_filter._get_pipeline(repo)

    assert [(name, kw) for name, _repo, kw in calls.from_pretrained] == [
        ("AutoTokenizer", {"local_files_only": True}),
        ("AutoModelForTokenClassification", {"local_files_only": True}),
    ]
    ((task, model, kwargs),) = calls.pipeline
    assert task == "token-classification"
    assert model == ("AutoModelForTokenClassification", repo)
    assert kwargs == {"tokenizer": ("AutoTokenizer", repo), "device": "cpu"}


def test_the_nsfw_classifier_loads_through_the_cache_first_and_keeps_every_label(
    calls, monkeypatch
):
    from topos.engine import model_cache
    from topos.sanitization import nsfw_classifier

    repo = nsfw_classifier.DEFAULT_NSFW_CLASSIFIER_MODEL
    monkeypatch.setattr(model_cache, "get_model_cache", lambda: _PassThroughCache())

    nsfw_classifier._get_pipeline(repo)

    assert [(name, kw) for name, _repo, kw in calls.from_pretrained] == [
        ("AutoTokenizer", {"local_files_only": True}),
        ("AutoModelForSequenceClassification", {"local_files_only": True}),
    ]
    ((task, model, kwargs),) = calls.pipeline
    assert task == "text-classification"
    assert model == ("AutoModelForSequenceClassification", repo)
    # top_k=None is what makes the classifier return both labels, not only the top one.
    assert kwargs == {"tokenizer": ("AutoTokenizer", repo), "top_k": None}


def test_settings_switch_off_the_safetensors_conversion_probe():
    """The `.bin` conversion thread is four un-timed Hub GETs `local_files_only` does not stop."""
    import topos.config.settings  # noqa: F401  (set at import, like the progress-bar default)

    assert os.environ.get("DISABLE_SAFETENSORS_CONVERSION") == "1"
