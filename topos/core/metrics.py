"""No-op metrics interface for Topos."""

from __future__ import annotations

from typing import Any, Dict, Optional


class MetricsClient:
    """No-op metrics client for early scaffolding."""

    def increment(self, name: str, value: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
        _ = (name, value, tags)

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        _ = (name, value, tags)

    def observe(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        _ = (name, value, tags)


metrics = MetricsClient()
