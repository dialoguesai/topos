"""Observability metrics (Sprint 07). In-memory counters; extend for Prometheus later."""

from __future__ import annotations

import threading
from typing import Dict


_counts: Dict[str, float] = {}
_lock = threading.Lock()


def record_metric(name: str, value: float) -> None:
    with _lock:
        _counts[name] = _counts.get(name, 0) + value


def get_metric(name: str) -> float:
    with _lock:
        return _counts.get(name, 0.0)


def reset_metrics() -> None:
    with _lock:
        _counts.clear()
