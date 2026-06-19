"""Owner-authenticated signal read APIs (Phase 2)."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_api_key
from ..features.signal.service import get_signal_service

logger = logging.getLogger("topos.api.signal")

router = APIRouter(prefix="/signal", tags=["signal"])


def _probe_adapters():
    from ..storage.adapters.factory import AdapterFactory

    try:
        return AdapterFactory.from_runtime()
    except Exception:
        return AdapterFactory.create("memory")


def _check_tier_vector() -> None:
    try:
        _probe_adapters().vector
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"error": "tier.vector_unavailable"}) from exc


def _check_tier_graph() -> None:
    try:
        _probe_adapters().graph
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"error": "tier.graph_unavailable"}) from exc


@router.get("/vectors/search")
async def search_signal_vectors(
    _api_key: str = Depends(require_api_key),
    q: str = Query(..., min_length=1, max_length=2000),
    limit: int = Query(20, ge=1, le=100),
    source_id: Optional[str] = None,
    dimension: Optional[str] = None,
    model: Optional[str] = None,
):
    """
    Semantic search over stored embeddings (metadata + similarity score; no raw vectors).

    Example: {"query": "project planning", "items": [{"embedding_id": "...", "similarity": 0.82}]}
    """
    import asyncio

    _check_tier_vector()
    service = get_signal_service()
    return await asyncio.to_thread(
        service.search_vectors,
        query=q,
        limit=limit,
        source_id=source_id,
        dimension=dimension,
        model=model,
    )


@router.get("/vectors/source-text")
async def get_vector_source_text(
    _api_key: str = Depends(require_api_key),
    record_id: str = Query(..., min_length=1),
):
    """Full canonical message text for a vector record_id (message_id)."""
    _check_tier_vector()
    service = get_signal_service()
    return service.get_vector_source_text(record_id=record_id)


@router.get("/vectors")
async def list_signal_vectors(
    _api_key: str = Depends(require_api_key),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source_id: Optional[str] = None,
    dimension: Optional[str] = None,
    model: Optional[str] = None,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
):
    """
    Paginated embedding metadata (no raw vector floats).

    Example: {"items": [{"embedding_id": "...", "dims": 384}], "total": 1}
    """
    _check_tier_vector()
    service = get_signal_service()
    return service.list_vectors(
        limit=limit,
        offset=offset,
        source_id=source_id,
        dimension=dimension,
        model=model,
        created_after=created_after,
        created_before=created_before,
    )


@router.get("/graph")
async def list_signal_graph(
    _api_key: str = Depends(require_api_key),
    dimension: Optional[str] = None,
    limit_nodes: int = Query(200, ge=1, le=1000),
    limit_edges: int = Query(500, ge=1, le=5000),
    edge_type: Optional[str] = None,
    min_weight: Optional[float] = None,
    source_id: Optional[str] = None,
):
    """Signal-layer graph nodes and edges."""
    _check_tier_graph()
    service = get_signal_service()
    return service.list_graph(
        dimension=dimension,
        limit_nodes=limit_nodes,
        limit_edges=limit_edges,
        edge_type=edge_type,
        min_weight=min_weight,
        source_id=source_id,
    )


@router.get("/dimensions")
async def list_signal_dimensions(_api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    return service.list_dimensions()


@router.get("/data-health")
async def get_signal_data_health(_api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    return service.get_data_health()


@router.get("/topic-clusters")
async def list_topic_clusters(
    limit: int = Query(default=50, ge=1, le=200),
    dimension: Optional[str] = Query(default=None),
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    return service.list_topic_clusters(limit=limit, dimension=dimension)


@router.get("/topic-clusters/{cluster_id}/members")
async def list_topic_cluster_members(
    cluster_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    return service.list_topic_cluster_members(cluster_id, limit=limit)
