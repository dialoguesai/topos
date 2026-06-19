"""Mode-aware signal retrieval (PRD §8.5–8.7)."""

from __future__ import annotations

from typing import Any, Dict, List

from ..storage.adapters.factory import AdapterBundle
from .manifest import ScopeResolutionManifest
from .types import (
    MODE_RANK,
    AccessMode,
    RetrievalBundle,
    RetrievalError,
    RetrievalRequest,
)

_INFERENCE_EXCLUDED_KEYS = frozenset({"content", "text", "body"})


def _mode_allowed(requested: AccessMode, ceiling: str) -> bool:
    req_rank = MODE_RANK.get(str(requested))
    ceil_rank = MODE_RANK.get(str(ceiling), 1)
    if req_rank is None:
        return False
    return req_rank <= ceil_rank


def _strip_forbidden(data: Any, forbidden: List[str]) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in forbidden or any(f in k for f in forbidden):
                continue
            out[k] = _strip_forbidden(v, forbidden)
        return out
    if isinstance(data, list):
        return [_strip_forbidden(item, forbidden) for item in data]
    return data


class DefaultSignalRetrievalAdapter:
    """Retrieve minimum necessary data per access mode and manifest."""

    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters
        self._last_stores: List[str] = []

    def stores_touched(self) -> List[str]:
        return list(self._last_stores)

    def retrieve(self, request: RetrievalRequest) -> RetrievalBundle:
        manifest: ScopeResolutionManifest = request.manifest
        if request.skip_retrieval:
            self._last_stores = []
            return RetrievalBundle(context_packet={}, stores_touched=[], record_counts={})

        if not _mode_allowed(request.access_mode, manifest.access_mode_ceiling):
            raise RetrievalError("mode_ceiling_exceeded", f"{request.access_mode} exceeds ceiling {manifest.access_mode_ceiling}")

        touched: List[str] = []
        counts: Dict[str, int] = {}
        packet: Dict[str, Any] = {"scope_id": manifest.scope_id, "access_mode": request.access_mode}

        mode = request.access_mode
        if mode == "raw":
            rows: List[Dict[str, Any]] = []
            for table in manifest.canonical_tables:
                page = self._adapters.canonical.list(table, limit=100, offset=0)
                touched.append("canonical")
                table_rows = list(page.items)
                counts[table] = len(table_rows)
                for row in table_rows:
                    rows.append({"_table": table, **row})
            packet["rows"] = _strip_forbidden(rows, manifest.must_not_retrieve)
        elif mode == "summary":
            summaries: List[Dict[str, Any]] = []
            for dim in manifest.primary_dimensions:
                dim_key = dim.lower()
                page = self._adapters.signal.get_by_dimension(dim_key, limit=50, offset=0)
                touched.append("signal")
                for item in page.items:
                    if item.get("summary_text") or item.get("topic") or item.get("dimension"):
                        summaries.append({k: v for k, v in item.items() if k != "content"})
            packet["summaries"] = summaries
            counts["summaries"] = len(summaries)
        elif mode == "inference":
            scores: List[Dict[str, Any]] = []
            for dim in manifest.primary_dimensions:
                page = self._adapters.signal.get_by_dimension(dim.lower(), limit=50, offset=0)
                touched.append("signal")
                for item in page.items:
                    scores.append({k: v for k, v in item.items() if k not in _INFERENCE_EXCLUDED_KEYS})
            graph = self._adapters.graph.list_graph(limit_nodes=50, limit_edges=100)
            if graph.get("edges") or graph.get("nodes"):
                touched.append("graph")
                packet["graph"] = {
                    "nodes": graph.get("nodes") or [],
                    "edges": graph.get("edges") or [],
                }
            meta = self._adapters.vector.list_metadata(limit=20, offset=0)
            if meta.total:
                touched.append("vector")
            packet["scores"] = _strip_forbidden(scores, manifest.must_not_retrieve)
            counts["scores"] = len(scores)
            packet = _strip_forbidden(packet, manifest.must_not_retrieve)

        self._last_stores = sorted(set(touched))
        return RetrievalBundle(context_packet=packet, stores_touched=self._last_stores, record_counts=counts)


# Protocol alias for imports
SignalRetrievalAdapter = DefaultSignalRetrievalAdapter
