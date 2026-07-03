"""Tests for Engine ModelCache LRU eviction."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from topos.engine.model_cache import ModelCache, ModelSlot, reset_model_cache_for_tests


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_model_cache_for_tests()
    yield
    reset_model_cache_for_tests()


def test_acquire_cache_hit():
    cache = ModelCache(max_resident=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return {"model": "a"}

    handle1, hit1 = cache.acquire(ModelSlot.NER, "dslim/bert-base-NER", loader)
    handle2, hit2 = cache.acquire(ModelSlot.NER, "dslim/bert-base-NER", loader)
    assert hit1 is False
    assert hit2 is True
    assert handle1 is handle2
    assert calls["n"] == 1


def test_model_id_change_reloads():
    cache = ModelCache(max_resident=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return f"model-{calls['n']}"

    _, hit1 = cache.acquire(ModelSlot.EMBEDDING, "model-a", loader)
    handle2, hit2 = cache.acquire(ModelSlot.EMBEDDING, "model-b", loader)
    assert hit1 is False
    assert hit2 is False
    assert handle2 == "model-2"
    assert calls["n"] == 2


def test_lru_eviction_when_at_capacity():
    cache = ModelCache(max_resident=2)
    order = []

    def make_loader(slot: ModelSlot):
        def _load():
            order.append(slot.value)
            return slot.value

        return _load

    cache.acquire(ModelSlot.URL_PIPELINE, "m1", make_loader(ModelSlot.URL_PIPELINE))
    cache.acquire(ModelSlot.NER, "m2", make_loader(ModelSlot.NER))
    cache.acquire(ModelSlot.EMOTION, "m3", make_loader(ModelSlot.EMOTION))
    assert ModelSlot.URL_PIPELINE.value not in cache.resident_slots()
    assert len(cache.resident_slots()) == 2
    assert cache.evictions_total >= 1


def test_trim_to_budget_respects_max():
    cache = ModelCache(max_resident=2)
    for slot in (ModelSlot.NER, ModelSlot.EMOTION, ModelSlot.SENTIMENT):
        cache.acquire(slot, slot.value, lambda s=slot: s.value)
    cache.trim_to_budget()
    assert len(cache.resident_slots()) <= 2


def test_evict_calls_release_ml_memory():
    cache = ModelCache(max_resident=1)
    with patch("topos.engine.model_cache.release_ml_memory") as release:
        cache.acquire(ModelSlot.NSFW, "nsfw-model", lambda: object())
        cache.acquire(ModelSlot.PRIVACY_FILTER, "privacy-model", lambda: object())
        assert release.call_count >= 1


def test_thread_safe_acquire():
    cache = ModelCache(max_resident=5)
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(20):
                cache.acquire(ModelSlot.EMBEDDING, "embed-model", lambda: [1.0, 2.0])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert ModelSlot.EMBEDDING.value in cache.resident_slots()
