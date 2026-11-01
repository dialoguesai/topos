"""Legacy facade backed by OTel; callers must supply content-free attributes."""

from __future__ import annotations

from typing import Any, Dict, Optional
from ..observability.metrics import measure


class MetricsClient:
    """Compatibility facade for counters, gauges and distributions."""

    def increment(self, name: str, value: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
        measure("counter", name, value, attributes=tags)

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        measure("gauge", name, value, attributes=tags)

    def observe(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        measure("histogram", name, value, attributes=tags)


metrics = MetricsClient()
