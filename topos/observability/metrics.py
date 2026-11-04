"""OTel instruments plus the backwards-compatible local scalar snapshots."""

from __future__ import annotations

import threading
from typing import Dict
from .runtime import current


_counts: Dict[str, float] = {}
_gauges: Dict[str, float] = {}
_lock = threading.Lock()


def measure(kind, name, value, *, unit="", attributes=None):
    """Instrumentation must never fail a product operation. Callers use fixed keys."""
    try:
        instrument = current().instrument(kind, name, unit)
        operation = {"counter": "add", "histogram": "record", "gauge": "set"}[kind]
        getattr(instrument, operation)(value, attributes=attributes or {})
    except Exception:
        pass


def record_metric(name: str, value: float) -> None:
    with _lock:
        _counts[name] = _counts.get(name, 0) + value
    # Export a closed vocabulary, never dynamic metric names or user text.
    if name == "engine.inference_duration_ms":
        measure("histogram", "topos.engine.inference.duration", value / 1000, unit="s")
    elif name in {"engine.task_completed", "engine.task_failed", "engine.task_deferred"}:
        measure("counter", "topos.engine.tasks", value, unit="{task}",
                attributes={"topos.task.outcome": name.removeprefix("engine.task_")})
    elif name == "engine.model_evictions_total":
        measure("counter", "topos.engine.model.evictions", value, unit="{eviction}")


def set_metric(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = value
    if name == "engine.models_resident":
        measure("gauge", "topos.engine.models.resident", value, unit="{model}")
    elif name == "engine.process_rss_mb":
        measure("gauge", "topos.engine.process.memory.peak", value * 1024 * 1024, unit="By")


def get_metric(name: str) -> float:
    with _lock:
        if name in _gauges:
            return _gauges[name]
        return _counts.get(name, 0.0)


def reset_metrics() -> None:
    with _lock:
        _counts.clear()
        _gauges.clear()
