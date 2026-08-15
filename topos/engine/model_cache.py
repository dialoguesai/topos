"""Central ML model cache with LRU eviction and memory budget."""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from .memory_utils import get_process_rss_mb, release_ml_memory

logger = logging.getLogger("topos.engine.model_cache")

_cache_singleton: Optional["ModelCache"] = None
_cache_lock = threading.Lock()


class ModelSlot(str, Enum):
    EMOTION = "emotion"
    NER = "ner"
    EMBEDDING = "embedding"
    SENTIMENT = "sentiment"
    NSFW = "nsfw"
    PRIVACY_FILTER = "privacy_filter"
    RERANKER = "reranker"
    SCOPE_HEAD = "scope_head"


_SUBTYPE_TO_SLOT: Dict[str, ModelSlot] = {
    "scope_classification": ModelSlot.SCOPE_HEAD,
    "scope_classification_batch": ModelSlot.SCOPE_HEAD,
    "emotion_classification": ModelSlot.EMOTION,
    "emo_27": ModelSlot.EMOTION,
    "emotion_classification_batch": ModelSlot.EMOTION,
    "entity_extraction": ModelSlot.NER,
    "entity_extraction_batch": ModelSlot.NER,
    "embedding": ModelSlot.EMBEDDING,
    "rerank": ModelSlot.RERANKER,
    "sentiment_classification": ModelSlot.SENTIMENT,
    "privacy_disclosure": ModelSlot.PRIVACY_FILTER,
    "content_nsfw_classification": ModelSlot.NSFW,
}


class _CacheEntry:
    __slots__ = ("handle", "model_id", "last_used", "loader")

    def __init__(self, handle: Any, model_id: str, loader: Callable[[], Any]) -> None:
        self.handle = handle
        self.model_id = model_id
        self.last_used = time.monotonic()
        self.loader = loader


class ModelCache:
    """Thread-safe LRU cache for resident HF / sanitization models."""

    def __init__(
        self,
        *,
        max_resident: int = 3,
        idle_ttl_sec: float = 0,
        rss_soft_limit_mb: float = 0,
    ) -> None:
        self._max_resident = max(1, int(max_resident))
        self._idle_ttl_sec = max(0.0, float(idle_ttl_sec))
        self._rss_soft_limit_mb = max(0.0, float(rss_soft_limit_mb))
        self._entries: Dict[ModelSlot, _CacheEntry] = {}
        self._lock = threading.Lock()
        #: Signals load completion; shares ``_lock``. Slots with a load in
        #: flight (marked in ``_loading``) are absent from ``_entries``.
        self._load_done = threading.Condition(self._lock)
        self._loading: Dict[ModelSlot, str] = {}
        self._evictions_total = 0

    def acquire(
        self,
        slot: ModelSlot,
        model_id: str,
        loader: Callable[[], Any],
    ) -> Tuple[Any, bool]:
        """Return (handle, cache_hit). Loads via loader when missing or model_id changed."""
        model_id = (model_id or "").strip() or slot.value
        with self._load_done:
            while True:
                entry = self._entries.get(slot)
                if entry is not None and entry.model_id == model_id:
                    entry.last_used = time.monotonic()
                    return entry.handle, True
                if slot not in self._loading:
                    break
                # Another thread is loading this slot: wait for its result
                # instead of racing a second load of the same model (the
                # duplicate "Loading privacy-filter" of 2026-08-07).
                self._load_done.wait()
            if entry is not None:
                self._entries.pop(slot, None)
                self._evict_unlocked(slot, entry)
            # Capacity counts resident entries only; a slot mid-load is in
            # neither map, so residency can briefly overshoot by the number of
            # concurrent loads. Acceptable: the alternative is the pre-fix
            # behaviour of serializing every load behind one cache-wide lock.
            self._ensure_capacity_unlocked(exclude=slot)
            self._loading[slot] = model_id
        # Load OUTSIDE the lock. Holding the cache-wide lock across a slow (or
        # wedged — hung MPS load, 2026-08-07) loader starves every model user
        # in the process, not just this slot's.
        try:
            handle = loader()
        except BaseException:
            with self._load_done:
                self._loading.pop(slot, None)
                self._load_done.notify_all()
            raise
        with self._load_done:
            self._loading.pop(slot, None)
            self._entries[slot] = _CacheEntry(handle, model_id, loader)
            self._load_done.notify_all()
        return handle, False

    def evict_slot(self, slot: ModelSlot) -> None:
        with self._lock:
            entry = self._entries.pop(slot, None)
            if entry is not None:
                self._evict_unlocked(slot, entry)

    def trim_to_budget(self) -> None:
        """Evict idle and excess entries; flush torch cache when over RSS soft limit."""
        with self._lock:
            if self._idle_ttl_sec > 0:
                now = time.monotonic()
                for slot in list(self._entries.keys()):
                    entry = self._entries.get(slot)
                    if entry and now - entry.last_used >= self._idle_ttl_sec:
                        self._evict_unlocked(slot, entry)
                        self._entries.pop(slot, None)
            while len(self._entries) > self._max_resident:
                self._evict_lru_unlocked()
            if self._rss_soft_limit_mb > 0:
                rss = get_process_rss_mb()
                if rss is not None and rss > self._rss_soft_limit_mb:
                    while len(self._entries) > 1:
                        self._evict_lru_unlocked()
                    release_ml_memory()

    def resident_slots(self) -> List[str]:
        with self._lock:
            return [slot.value for slot in self._entries.keys()]

    @property
    def evictions_total(self) -> int:
        return self._evictions_total

    @property
    def max_resident(self) -> int:
        return self._max_resident

    def slot_for_subtype(self, subtype: str) -> Optional[ModelSlot]:
        return _SUBTYPE_TO_SLOT.get((subtype or "").strip())

    def touch_for_task(self, subtype: str, model_id: str) -> Optional[bool]:
        """Return cache_hit if slot is resident for subtype/model, else None."""
        slot = self.slot_for_subtype(subtype)
        if slot is None:
            return None
        model_id = (model_id or "").strip() or slot.value
        with self._lock:
            entry = self._entries.get(slot)
            if entry is not None and entry.model_id == model_id:
                entry.last_used = time.monotonic()
                return True
            if entry is not None:
                return False
            return False

    def _ensure_capacity_unlocked(self, *, exclude: Optional[ModelSlot] = None) -> None:
        while len(self._entries) >= self._max_resident:
            evicted = self._evict_lru_unlocked(exclude=exclude)
            if not evicted:
                break

    def _evict_lru_unlocked(self, *, exclude: Optional[ModelSlot] = None) -> bool:
        candidates = [
            (slot, entry)
            for slot, entry in self._entries.items()
            if slot != exclude
        ]
        if not candidates:
            return False
        slot, entry = min(candidates, key=lambda item: item[1].last_used)
        self._entries.pop(slot, None)
        self._evict_unlocked(slot, entry)
        return True

    def _evict_unlocked(self, slot: ModelSlot, entry: _CacheEntry) -> None:
        entry.handle = None
        self._evictions_total += 1
        try:
            from ..observability.metrics import record_metric

            record_metric("engine.model_evictions_total", 1.0)
        except Exception:
            pass
        logger.debug("Evicted model slot=%s model_id=%s", slot.value, entry.model_id)
        release_ml_memory()


def _settings_policy() -> Tuple[int, float, float]:
    try:
        from ..config.settings import settings

        return (
            int(getattr(settings, "engine_max_resident_models", 3) or 3),
            float(getattr(settings, "engine_model_idle_ttl_sec", 0) or 0),
            float(getattr(settings, "engine_memory_rss_soft_limit_mb", 0) or 0),
        )
    except Exception:
        return 3, 0.0, 0.0


def get_model_cache() -> ModelCache:
    global _cache_singleton
    if _cache_singleton is not None:
        return _cache_singleton
    with _cache_lock:
        if _cache_singleton is None:
            max_res, idle_ttl, rss_limit = _settings_policy()
            _cache_singleton = ModelCache(
                max_resident=max_res,
                idle_ttl_sec=idle_ttl,
                rss_soft_limit_mb=rss_limit,
            )
        return _cache_singleton


def reset_model_cache_for_tests() -> None:
    global _cache_singleton
    with _cache_lock:
        if _cache_singleton is not None:
            for slot in list(_cache_singleton._entries.keys()):
                _cache_singleton.evict_slot(slot)
        _cache_singleton = None
