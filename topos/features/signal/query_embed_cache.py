"""Request-scoped query embedding cache."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from typing import Dict, List, Optional, Tuple

_cache_var: ContextVar[Optional[Dict[str, List[float]]]] = ContextVar("query_embed_cache", default=None)
_session_cache: Dict[str, Tuple[List[float], float]] = {}
_SESSION_TTL_SEC = 300.0
_SESSION_MAX = 32


def _cache() -> Dict[str, List[float]]:
    store = _cache_var.get()
    if store is None:
        store = {}
        _cache_var.set(store)
    return store


def cache_key(text: str, *, model: str) -> str:
    digest = hashlib.sha256(f"{model}:{text}".encode("utf-8")).hexdigest()
    return digest


def get_cached_query_embedding(text: str, *, model: str) -> Optional[List[float]]:
    key = cache_key(text, model=model)
    hit = _cache().get(key)
    if hit is not None:
        return list(hit)
    import time

    session_hit = _session_cache.get(key)
    if session_hit is not None:
        vector, ts = session_hit
        if time.monotonic() - ts <= _SESSION_TTL_SEC:
            return list(vector)
        _session_cache.pop(key, None)
    return None


def set_cached_query_embedding(text: str, *, model: str, vector: List[float]) -> None:
    import time

    key = cache_key(text, model=model)
    _cache()[key] = list(vector)
    if len(_session_cache) >= _SESSION_MAX:
        oldest = min(_session_cache.items(), key=lambda item: item[1][1])[0]
        _session_cache.pop(oldest, None)
    _session_cache[key] = (list(vector), time.monotonic())


def clear_query_embed_cache() -> None:
    _cache_var.set({})
