"""Signal read service shared by HTTP and WS handlers."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ...storage.adapters.factory import AdapterBundle
from .data_health import DataHealthComputer
from .dimension_registry import MVP_DIMENSIONS
from .schemas import strip_vector_fields

_DATA_HEALTH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_DATA_HEALTH_TTL_SEC = 30.0


class SignalService:
    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters

    def list_vectors(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        page = self._adapters.vector.list_metadata(
            source_id=source_id,
            dimension=dimension,
            model=model,
            limit=limit,
            offset=offset,
        )
        items = [strip_vector_fields(item) for item in page.items]
        if created_after or created_before:
            filtered = []
            for item in items:
                ts = item.get("created_at") or ""
                if created_after and ts < created_after:
                    continue
                if created_before and ts > created_before:
                    continue
                filtered.append(item)
            items = filtered
        total = len(items) if (created_after or created_before) else page.total
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def list_graph(
        self,
        *,
        dimension: Optional[str] = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
        edge_type: Optional[str] = None,
        min_weight: Optional[float] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        limit_nodes = max(1, min(int(limit_nodes), 1000))
        limit_edges = max(1, min(int(limit_edges), 2000))
        graph = self._adapters.graph.list_graph(
            dimension=dimension,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
        )
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if source_id:
            nodes = [n for n in nodes if n.get("source_id") == source_id]
            edges = [e for e in edges if e.get("source_id") == source_id]
        if edge_type:
            edges = [e for e in edges if e.get("edge_type") == edge_type]
        if min_weight is not None:
            edges = [e for e in edges if float(e.get("weight") or 0) >= min_weight]
        return {"nodes": nodes, "edges": edges}

    def _cached_profiles(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        cache_key = ",".join(sorted(deferred_jobs or []))
        now = time.monotonic()
        cached = _DATA_HEALTH_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _DATA_HEALTH_TTL_SEC:
            return cached[1]
        profiles = DataHealthComputer(self._adapters).compute(deferred_jobs=deferred_jobs)
        _DATA_HEALTH_CACHE[cache_key] = (now, profiles)
        return profiles

    def list_dimensions(self) -> Dict[str, Any]:
        profiles = self._cached_profiles()
        dimensions = []
        for dim in MVP_DIMENSIONS:
            p = profiles.get(dim["id"], {})
            dimensions.append(
                {
                    "id": dim["id"],
                    "label": dim["label"],
                    "coverage_score": p.get("coverage_score", 0.0),
                    "freshness_score": p.get("freshness_score", 0.0),
                    "canonical_sources": p.get("canonical_sources", []),
                    "updated_at": p.get("updated_at"),
                }
            )
        return {"dimensions": dimensions}

    def get_data_health(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Any]:
        profiles = self._cached_profiles(deferred_jobs=deferred_jobs)
        ollama_up = "ollama_unreachable" not in {
            f for p in profiles.values() for f in p.get("provider_failures") or []
        }
        return {
            "dimensions": list(profiles.values()),
            "provider_status": {"ollama": "up" if ollama_up else "down", "huggingface": "up"},
            "updated_at": profiles.get("memory", {}).get("updated_at"),
        }


def get_signal_service(conn=None) -> SignalService:
    from ...storage.adapters.factory import AdapterFactory

    if conn is None:
        from ...core.state import get_db_connection

        conn = get_db_connection()
    if conn is not None:
        bundle = AdapterFactory.create("local_database", conn=conn)
    else:
        bundle = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
    return SignalService(bundle)
