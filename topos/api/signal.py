"""Owner-authenticated signal read APIs (Phase 2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

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
    mode: str = Query("hybrid", pattern="^(vector|hybrid)$"),
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
    hydrate: bool = Query(False),
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
        mode=mode,
        event_after=event_after,
        event_before=event_before,
        hydrate=hydrate,
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
    try:
        return service.list_topic_cluster_members(cluster_id, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class BriefUpdateBody(BaseModel):
    markdown_body: str = Field(..., min_length=0, max_length=50000)


class SignalObjectOverrideBody(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)


class FitEvaluateBody(BaseModel):
    opportunity_type: str = Field(..., min_length=1)
    context: Dict[str, Any] = Field(default_factory=dict)


@router.post("/fit/evaluate")
async def evaluate_fit(body: FitEvaluateBody, _api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    try:
        return service.evaluate_fit(body.opportunity_type, context=body.context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _entities_conn():
    from ..core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database_unavailable")
    return conn


@router.get("/entities")
async def list_entities(
    q: Optional[str] = Query(default=None, max_length=200),
    entity_type: Optional[str] = Query(default=None, max_length=40),
    contacts_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _api_key: str = Depends(require_api_key),
):
    """Resolved entity registry (entity spine), sorted by mention count."""
    from ..features.entities.reads import list_entities as _list

    return _list(
        _entities_conn(),
        q=q,
        entity_type=entity_type,
        contacts_only=contacts_only,
        limit=limit,
        offset=offset,
    )


# NB: must be declared before /entities/{entity_id} so "graph" isn't captured
# as an entity id.
@router.get("/entities/graph")
async def get_entity_graph(
    limit_nodes: int = Query(default=100, ge=1, le=500),
    limit_edges: int = Query(default=300, ge=1, le=1500),
    min_weight: float = Query(default=0.0, ge=0.0),
    include_closed: bool = Query(default=False),
    as_of: Optional[str] = Query(default=None),
    _api_key: str = Depends(require_api_key),
):
    """Entity-spine graph (decayed typed edges) in list_graph node/edge shape.

    Each edge carries valid_from/valid_to/last_event_at. ``as_of`` (ISO date)
    returns the graph as it stood at that instant (temporal scrubber);
    ``include_closed`` adds ended edges to the present view.
    """
    from ..features.entities.reads import entity_graph

    return entity_graph(
        _entities_conn(),
        limit_nodes=limit_nodes,
        limit_edges=limit_edges,
        min_weight=min_weight,
        include_closed=include_closed,
        as_of=as_of,
    )


@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: str,
    _api_key: str = Depends(require_api_key),
):
    """Entity detail: aliases, connections, recent mentions, dossier (owner view)."""
    from ..features.entities.reads import get_entity_detail

    detail = get_entity_detail(_entities_conn(), entity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"entity not found: {entity_id}")
    return detail


@router.get("/entity-review")
async def list_entity_review(
    status: str = Query(default="pending", max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    """Pending entity merge candidates (owner curation queue)."""
    from ..features.entities.consolidation import list_review

    items = list_review(_entities_conn(), status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.post("/entity-review/sweep")
async def run_entity_review_sweep(_api_key: str = Depends(require_api_key)):
    """Run the consolidation sweep now; returns proposal counts."""
    from ..features.entities.consolidation import propose_merges

    return propose_merges(_entities_conn())


@router.post("/entity-review/{review_id}/approve")
async def approve_entity_review(review_id: str, _api_key: str = Depends(require_api_key)):
    from ..features.entities.consolidation import resolve_review

    try:
        return resolve_review(_entities_conn(), review_id, action="approve")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/entity-review/{review_id}/dismiss")
async def dismiss_entity_review(review_id: str, _api_key: str = Depends(require_api_key)):
    from ..features.entities.consolidation import resolve_review

    try:
        return resolve_review(_entities_conn(), review_id, action="dismiss")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/entities/{entity_id}/exclude")
async def exclude_entity_from_intelligence(
    entity_id: str,
    _api_key: str = Depends(require_api_key),
):
    """Owner exclusion: stop tracking this entity (tombstoned, reversible)."""
    from ..features.lifecycle.exclusions import ExclusionStore

    try:
        return ExclusionStore(_entities_conn()).exclude_entity(entity_ref=entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/facts")
async def list_facts(
    predicate: Optional[str] = Query(default=None, max_length=80),
    dimension: Optional[str] = Query(default=None, max_length=40),
    include_closed: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _api_key: str = Depends(require_api_key),
):
    """Atomic owner facts with temporal validity (belief history via include_closed)."""
    from ..features.facts.reads import list_facts as _list

    return _list(
        _entities_conn(),
        predicate=predicate,
        dimension=dimension,
        include_closed=include_closed,
        limit=limit,
        offset=offset,
    )


@router.get("/insights")
async def list_stat_insights(
    dimension: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=200, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    """Promoted statistical insights (owner-only rhythms, session stats, trends)."""
    from ..features.facts.reads import list_stat_insights as _list

    return _list(_entities_conn(), dimension=dimension, limit=limit)


@router.get("/timeline")
async def list_timeline(
    canonical_table: Optional[str] = Query(default=None, max_length=60),
    source_id: Optional[str] = Query(default=None, max_length=120),
    date_from: Optional[str] = Query(default=None, max_length=32),
    date_to: Optional[str] = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _api_key: str = Depends(require_api_key),
):
    """Unified temporal projection across canonical tables (newest first)."""
    from ..features.facts.reads import list_timeline as _list

    return _list(
        _entities_conn(),
        canonical_table=canonical_table,
        source_id=source_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/definitions")
async def list_signal_definitions(_api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    return service.list_definitions()


@router.get("/definitions/{dimension}")
async def get_signal_definition(dimension: str, _api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    try:
        return service.get_definition(dimension)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/objects")
async def list_signal_objects(
    dimension: str = Query(..., min_length=1),
    object_type: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    try:
        return service.list_signal_objects(dimension, object_type=object_type, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/objects/{object_id}")
async def get_signal_object(object_id: str, _api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    try:
        return service.get_signal_object(object_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/objects/{object_id}/owner_override")
async def owner_override_signal_object(
    object_id: str,
    body: SignalObjectOverrideBody,
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    try:
        return service.owner_override_signal_object(object_id, body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/briefs")
async def list_dimension_briefs(_api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    return service.list_briefs()


@router.get("/briefs/{dimension}")
async def get_dimension_brief(dimension: str, _api_key: str = Depends(require_api_key)):
    service = get_signal_service()
    try:
        return service.get_brief(dimension)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/briefs/{dimension}")
async def update_dimension_brief(
    dimension: str,
    body: BriefUpdateBody,
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    try:
        return service.update_brief(dimension, body.markdown_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/briefs/{dimension}/revisions")
async def list_dimension_brief_revisions(
    dimension: str,
    limit: int = Query(default=20, ge=1, le=100),
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    try:
        return service.list_brief_revisions(dimension, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/briefs/{dimension}/refresh")
async def refresh_dimension_brief(
    dimension: str,
    limit: int = Query(default=40, ge=1, le=100),
    _api_key: str = Depends(require_api_key),
):
    service = get_signal_service()
    try:
        return await service.refresh_brief(dimension, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
