"""Data Health lite computation for MVP dimensions."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...storage.adapters.factory import AdapterBundle
from .dimension_registry import DIMENSION_SIGNAL_OBJECTS, MVP_DIMENSIONS


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def freshness_score(latest_ts: Optional[str], *, half_life_hours: float = 24.0) -> float:
    """Decay from max signal created_at vs now (24h half-life default)."""
    ts = _parse_ts(latest_ts)
    if ts is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return float(math.pow(0.5, age_hours / half_life_hours))


class DataHealthComputer:
    """
    coverage_score = fraction of canonical-backed signal objects present for dimension.
    Documented MVP formula: min(1.0, signal_row_count / max(1, expected_objects)).
    """

    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters

    def compute(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        deferred_jobs = deferred_jobs or []
        provider_failures: List[str] = []
        if any(j in deferred_jobs for j in ("topics", "dimension_summary", "goal_extraction")):
            provider_failures.append("ollama_unreachable")

        profiles: Dict[str, Dict[str, Any]] = {}
        for dim in MVP_DIMENSIONS:
            dim_id = dim["id"]
            signal_count = 0
            latest = None
            try:
                page = self._adapters.signal.get_by_dimension(dim_id, limit=1000, offset=0)
                signal_count += page.total
                for item in page.items:
                    ts = item.get("created_at")
                    if ts and (latest is None or ts > latest):
                        latest = ts
            except Exception:
                pass
            if dim_id == "memory":
                vpage = self._adapters.vector.list_metadata(limit=1000, offset=0, dimension=dim_id)
                signal_count += vpage.total
            expected = max(1, len(DIMENSION_SIGNAL_OBJECTS.get(dim_id, [])))
            coverage = min(1.0, signal_count / expected)
            profiles[dim_id] = {
                "id": dim_id,
                "label": dim["label"],
                "coverage_score": coverage,
                "freshness_score": freshness_score(latest),
                "canonical_sources": DIMENSION_SIGNAL_OBJECTS.get(dim_id, []),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "provider_failures": list(provider_failures),
            }
        return profiles
