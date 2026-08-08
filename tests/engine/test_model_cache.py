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


def test_slow_load_does_not_block_other_slots():
    """One slow (or wedged) model load must starve only its own slot, never
    every model user in the process — holding the cache-wide lock across a
    hung MPS load was one arm of the 2026-08-07 deadlock."""
    cache = ModelCache(max_resident=5)
    slow_started = threading.Event()
    slow_release = threading.Event()
    slow_done = threading.Event()

    def slow_loader():
        slow_started.set()
        assert slow_release.wait(timeout=10)
        return "slow-model"

    def run_slow():
        cache.acquire(ModelSlot.PRIVACY_FILTER, "privacy-model", slow_loader)
        slow_done.set()

    thread = threading.Thread(target=run_slow)
    thread.start()
    try:
        assert slow_started.wait(timeout=5)
        # While the privacy-filter load is stuck, another slot must load freely.
        handle, hit = cache.acquire(ModelSlot.EMBEDDING, "embed-model", lambda: "embed")
        assert handle == "embed"
        assert hit is False
        assert not slow_done.is_set()
    finally:
        slow_release.set()
        thread.join(timeout=10)
    assert slow_done.is_set()
    assert ModelSlot.PRIVACY_FILTER.value in cache.resident_slots()


def test_concurrent_same_slot_acquires_load_once():
    cache = ModelCache(max_resident=5)
    calls = {"n": 0}
    first_in = threading.Event()
    release = threading.Event()

    def loader():
        calls["n"] += 1
        first_in.set()
        assert release.wait(timeout=10)
        return {"model": "shared"}

    results: list = []

    def worker():
        results.append(cache.acquire(ModelSlot.NER, "ner-model", loader))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert first_in.wait(timeout=5)
    t2.start()
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert calls["n"] == 1, "second thread must wait for the in-flight load, not race it"
    assert len(results) == 2
    assert results[0][0] is results[1][0]
    assert {hit for _, hit in results} == {True, False}


def test_failed_load_unblocks_waiters():
    cache = ModelCache(max_resident=5)

    with pytest.raises(RuntimeError, match="load failed"):
        cache.acquire(ModelSlot.NSFW, "nsfw-model", lambda: (_ for _ in ()).throw(RuntimeError("load failed")))

    # The slot must not be stuck in 'loading': a retry runs the loader again.
    handle, hit = cache.acquire(ModelSlot.NSFW, "nsfw-model", lambda: "recovered")
    assert handle == "recovered"
    assert hit is False


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
