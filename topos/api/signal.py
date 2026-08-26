"""Owner-authenticated signal read APIs (Phase 2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
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
    health = service.get_data_health()
    # Derived data that a failed job still owes. Surfaced on the health route
    # because "a batch silently lost its facts" is exactly the condition this
    # endpoint exists to catch, and it was invisible until now.
    try:
        from ..core.state import get_db_connection
        from ..enrichment.derivation_recovery import pending_derivation_summary

        if isinstance(health, dict):
            health["derivation_debt"] = pending_derivation_summary(get_db_connection())
    except Exception as exc:  # noqa: BLE001 — health must never 500
        logger.debug("derivation debt summary skipped: %s", exc)
    return health


@router.get("/derivation-debt")
async def get_derivation_debt(_api_key: str = Depends(require_api_key)):
    """Derivation jobs that failed and still owe their output."""
    from ..core.state import get_db_connection
    from ..enrichment.derivation_recovery import (
        list_pending_derivation_retries,
        pending_derivation_summary,
    )

    conn = get_db_connection()
    return {
        "summary": pending_derivation_summary(conn),
        "pending": list_pending_derivation_retries(conn, limit=200),
    }


@router.post("/derivation-debt/retry")
async def retry_derivation_debt(
    dry_run: bool = Query(default=False),
    source_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    _api_key: str = Depends(require_api_key),
):
    """Re-run failed derivations. ``dry_run`` reports without writing."""
    from ..enrichment.derivation_recovery import retry_pending_derivations

    return await retry_pending_derivations(source_id=source_id, limit=limit, dry_run=dry_run)


@router.get("/topic-clusters")
async def list_topic_clusters(
    limit: int = Query(default=50, ge=1, le=200),
    dimension: Optional[str] = Query(default=None),
    _api_key: str = Depends(require_api_key),
):
    # Local API-key route: owner identity is established by the transport, so
    # the owner sees their own protected clusters here (same rule as the entity
    # registry above). The guard still exists so this route cannot drift into
    # serving a non-owner unfiltered.
    from ..features.lifecycle.blackhole_guard import owner_ui_guard
    from ..core.state import get_db_connection

    service = get_signal_service()
    return service.list_topic_clusters(
        guard=owner_ui_guard(get_db_connection()), limit=limit, dimension=dimension
    )


@router.get("/topic-clusters/{cluster_id}/members")
async def list_topic_cluster_members(
    cluster_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    from ..features.lifecycle.blackhole_guard import owner_ui_guard
    from ..core.state import get_db_connection

    service = get_signal_service()
    try:
        return service.list_topic_cluster_members(
            cluster_id, guard=owner_ui_guard(get_db_connection()), limit=limit
        )
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
    from ..features.lifecycle.blackhole_guard import owner_ui_guard

    conn = _entities_conn()
    return _list(
        conn,
        guard=owner_ui_guard(conn),
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
    limit_nodes: int = Query(default=100, ge=1, le=5000),
    limit_edges: int = Query(default=300, ge=1, le=20000),
    min_weight: float = Query(default=0.0, ge=0.0),
    include_closed: bool = Query(default=False),
    as_of: Optional[str] = Query(default=None),
    selection: str = Query(default="weight"),
    offset: int = Query(default=0, ge=0),
    _api_key: str = Depends(require_api_key),
):
    """Entity-spine graph (decayed typed edges) in list_graph node/edge shape.

    Each edge carries valid_from/valid_to/last_event_at. ``as_of`` (ISO date)
    returns the graph as it stood at that instant (temporal scrubber);
    ``include_closed`` adds ended edges to the present view.

    ``selection=weight`` (default) ranks by weight for MCP/minimal slices;
    ``selection=all`` ranks by recency for the owner knowledge-graph UI.
    """
    import asyncio

    from ..features.entities.reads import entity_graph
    from ..features.lifecycle.blackhole_guard import owner_ui_guard

    conn = _entities_conn()
    return await asyncio.to_thread(
        entity_graph,
        conn,
        guard=owner_ui_guard(conn),
        limit_nodes=limit_nodes,
        limit_edges=limit_edges,
        min_weight=min_weight,
        include_closed=include_closed,
        as_of=as_of,
        selection=selection,
        offset=offset,
    )


@router.post("/entities/graph/rebuild")
async def rebuild_graph(
    reextract: bool = Query(default=False),
    _api_key: str = Depends(require_api_key),
):
    """Rebuild the entity graph from existing derived data (no NER re-run).

    Cheap and safe: recomputes co_occurrence + communicates_with edges from the
    resolved ``entity_mentions`` set, materializes facts + topic clusters from
    ``signal_objects`` into labeled temporal edges, recounts mentions, prunes
    orphans, and refreshes dossiers. Returns a before/after edge-count report
    (co_occurrence / communicates_with / topic_edges / fact_edges).

    Bounded by the current mentions + signal_objects; to grow the mention set
    (pick up entities from records never processed), re-run the ``entities``
    enrichment job via ``POST /enrichment/process`` with ``force_reprocess=true``.
    ``reextract`` is accepted for forward-compatibility.
    """
    import asyncio

    from ..features.entities.rebuild_subprocess import run_graph_rebuild
    from ..storage.db.write_gate import WriteGateDeferred

    _entities_conn()  # fail fast with 503 before spawning the worker

    def _rebuild():
        # run_graph_rebuild sends a file-backed rebuild to a SUBPROCESS: even
        # off the loop in to_thread, the in-process compute (goal embeddings,
        # role map, Louvain) monopolized the GIL and starved the event loop
        # for ~103s (2026-08-08). This worker thread only waits on the child.
        # It still uses its OWN thread-local connection for the in-memory
        # fallback, never the loop thread's.
        from ..core.state import close_thread_db_connection

        try:
            return run_graph_rebuild(_entities_conn())
        finally:
            # to_thread reuses pooled threads; don't leak a connection per run.
            close_thread_db_connection()

    try:
        return await asyncio.to_thread(_rebuild)
    except WriteGateDeferred as exc:
        # Another rebuild (timer- or endpoint-triggered) holds the advisory
        # lock; interleaving two full rebuilds helps nobody.
        raise HTTPException(status_code=409, detail=f"graph rebuild already running: {exc}")


@router.get("/entities/graph/search")
async def search_entity_graph(
    q: str = Query(..., min_length=1, max_length=500),
    limit_records: int = Query(default=40, ge=1, le=100),
    limit_entities: int = Query(default=30, ge=1, le=100),
    event_after: Optional[str] = Query(default=None),
    event_before: Optional[str] = Query(default=None),
    _api_key: str = Depends(require_api_key),
):
    """Semantic graph search: rank entities by hybrid vector+FTS evidence.

    Reuses the vectors search pipeline (query embed → ANN + FTS RRF over
    signal_embeddings) then joins the scored records onto graph entities via
    entity_mentions; materialized goal/topic/conversation nodes also match by
    label. ``event_after/before`` pair with the graph's time window. Returns
    {entities: [{entity_id, label, entity_type, score, evidence[]}], …}.
    """
    import asyncio

    from ..features.entities.graph_search import graph_search
    from ..features.signal.service import get_signal_service

    _check_tier_vector()
    service = get_signal_service()
    conn = _entities_conn()

    def _run():
        def search_fn(*, query, limit, event_after=None, event_before=None):
            return service.search_vectors(
                query=query,
                limit=limit,
                mode="hybrid",
                event_after=event_after,
                event_before=event_before,
            )

        return graph_search(
            conn,
            query=q,
            search_fn=search_fn,
            limit_records=limit_records,
            limit_entities=limit_entities,
            event_after=event_after,
            event_before=event_before,
        )

    return await asyncio.to_thread(_run)


@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: str,
    _api_key: str = Depends(require_api_key),
):
    """Entity detail: aliases, connections, recent mentions, dossier (owner view)."""
    from ..features.entities.reads import get_entity_detail
    from ..features.lifecycle.blackhole_guard import owner_ui_guard

    conn = _entities_conn()
    detail = get_entity_detail(conn, entity_id, guard=owner_ui_guard(conn))
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
    from ..features.entities.consolidation import count_review, list_review

    conn = _entities_conn()
    items = list_review(conn, status=status, limit=limit)
    # ``total`` is the whole queue, not this page — counting ``items`` would
    # report the limit back as if it were the backlog.
    return {"items": items, "total": count_review(conn, status=status)}


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


# ---------------------------------------------------------------- affinity


class AffinityConfigBody(BaseModel):
    percentile: Optional[float] = Field(default=None, ge=90.0, le=99.9)
    nudge: Optional[str] = Field(default=None, pattern="^(fewer|more|ok)$")


class AffinityLabelBody(BaseModel):
    a: str = Field(..., min_length=1, max_length=80)
    b: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., pattern="^(useful|obvious|wrong|same_person)$")
    note: Optional[str] = Field(default=None, max_length=500)
    cosine: Optional[float] = Field(default=None, ge=0.0, le=1.0)


@router.get("/affinity/status")
async def get_affinity_status_api(_api_key: str = Depends(require_api_key)):
    """Last rebuild + current percentile + plain-language verdict."""
    from ..features.entities.affinity_owner import get_affinity_status

    return get_affinity_status(_entities_conn())


@router.put("/affinity/config")
async def put_affinity_config(
    body: AffinityConfigBody,
    _api_key: str = Depends(require_api_key),
):
    """Set P directly or nudge via too-few / looks-good / too-many."""
    from ..features.entities.affinity_owner import apply_affinity_config

    if body.percentile is None and body.nudge is None:
        raise HTTPException(
            status_code=400, detail="provide percentile or nudge (fewer|more|ok)"
        )
    return apply_affinity_config(
        _entities_conn(),
        percentile=body.percentile,
        nudge=body.nudge,  # type: ignore[arg-type]
    )


@router.post("/affinity/recompute")
async def post_affinity_recompute(_api_key: str = Depends(require_api_key)):
    """Rebuild context centroids then affinity edges now."""
    import asyncio

    from ..features.entities.affinity_owner import recompute_affinity_now

    return await asyncio.to_thread(recompute_affinity_now, _entities_conn())


@router.get("/affinity/pairs")
async def list_affinity_pairs(
    limit: int = Query(default=50, ge=1, le=200),
    _api_key: str = Depends(require_api_key),
):
    """Active unlabeled affinity edges + suppressed near-misses for review."""
    from ..features.entities.affinity_owner import list_affinity_pairs_for_review

    return list_affinity_pairs_for_review(_entities_conn(), limit=limit)


@router.post("/affinity/pairs/label")
async def post_affinity_pair_label(
    body: AffinityLabelBody,
    _api_key: str = Depends(require_api_key),
):
    from ..features.entities.affinity_owner import label_affinity_pair

    try:
        return label_affinity_pair(
            _entities_conn(),
            entity_a=body.a,
            entity_b=body.b,
            label=body.label,
            note=body.note,
            cosine=body.cosine,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class EntitySplitBody(BaseModel):
    surface: str = Field(..., min_length=1, max_length=200)


@router.post("/entities/{entity_id}/split")
async def split_entity_surface(
    entity_id: str,
    body: EntitySplitBody,
    _api_key: str = Depends(require_api_key),
):
    """Owner unbind: split a surface's mentions out of this entity into a
    fresh one, with a permanent no-bind guard so the resolver never re-merges
    the pair (accidental-merge reversal; see consolidation.split_surface)."""
    from ..features.entities.consolidation import split_surface

    try:
        return split_surface(_entities_conn(), entity_id, body.surface)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class EntityMergeBody(BaseModel):
    absorb_entity_id: str = Field(..., min_length=1, max_length=80)


@router.post("/entities/{entity_id}/merge")
async def merge_entity_into(
    entity_id: str,
    body: EntityMergeBody,
    _api_key: str = Depends(require_api_key),
):
    """Owner link: merge another entity into this one (drawer-driven inverse
    of split; see consolidation.merge_entity_pair)."""
    from ..features.entities.consolidation import merge_entity_pair

    try:
        return merge_entity_pair(_entities_conn(), entity_id, body.absorb_entity_id)
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


class EntityBlackholeBody(BaseModel):
    processing_tier: str = Field(default="secure", max_length=20)
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/entities/{entity_id}/blackhole")
async def blackhole_entity(
    entity_id: str,
    body: EntityBlackholeBody = EntityBlackholeBody(),
    _api_key: str = Depends(require_api_key),
):
    """Owner black hole: this entity is mine alone.

    Nothing is deleted — the owner keeps full visibility. Every other caller
    (third-party MCP agents, routines, grantees) is denied, and content
    mentioning it may only be processed by the tier's secure model set.
    Raises a rebuild-needed notification before the rebuild runs (D4).
    """
    import asyncio

    from ..features.lifecycle.blackhole import BlackholeStore
    from ..features.lifecycle.blackhole_rebuild import rebuild_for_blackhole

    conn = _entities_conn()
    try:
        result = BlackholeStore(conn).blackhole_entity(
            entity_ref=entity_id,
            processing_tier=body.processing_tier,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # D4: the notification above was raised first, so the owner already knows the
    # hide is incomplete; now do the rebuild that makes it true. Non-owners are
    # withheld the prose artifacts for the duration, so this running late is safe
    # while it running silently would not be.
    if not result.get("already_blackholed"):
        result["rebuild"] = await asyncio.to_thread(rebuild_for_blackhole, conn, entity_id)
        result["rebuild"] = result["rebuild"].as_dict()
    return result


@router.delete("/entities/{entity_id}/blackhole")
async def unblackhole_entity(
    entity_id: str,
    _api_key: str = Depends(require_api_key),
):
    """Lift a black hole. Existing grants are not restored — normal permissions resume."""
    from ..features.lifecycle.blackhole import BlackholeStore

    return BlackholeStore(_entities_conn()).unblackhole_entity(entity_ref=entity_id)


@router.get("/blackholes")
async def list_blackholes(_api_key: str = Depends(require_api_key)):
    """The owner's off-limits list (mirrors the exclusions list in the drawer)."""
    from ..features.lifecycle.blackhole import BlackholeStore

    store = BlackholeStore(_entities_conn())
    return {"blackholes": store.list(), "notifications": store.notifications(state="open")}


@router.post("/blackholes/notifications/{notification_id}/dismiss")
async def dismiss_blackhole_notification(
    notification_id: str,
    _api_key: str = Depends(require_api_key),
):
    from ..features.lifecycle.blackhole import BlackholeStore

    return {"dismissed": BlackholeStore(_entities_conn()).dismiss_notification(notification_id)}


@router.get("/facts")
async def list_facts(
    predicate: Optional[str] = Query(default=None, max_length=80),
    dimension: Optional[str] = Query(default=None, max_length=40),
    pack: Optional[str] = Query(default=None, max_length=60),
    include_closed: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _api_key: str = Depends(require_api_key),
):
    """Atomic owner facts with temporal validity (belief history via include_closed)."""
    from ..features.facts.reads import list_facts as _list
    from ..features.lifecycle.blackhole_guard import owner_ui_guard

    conn = _entities_conn()
    return _list(
        conn,
        guard=owner_ui_guard(conn),
        predicate=predicate,
        dimension=dimension,
        pack=pack,
        include_closed=include_closed,
        limit=limit,
        offset=offset,
    )


@router.post("/facts/verdict")
async def post_fact_verdict(
    object_id: str = Body(...),
    action: str = Body(..., description="confirm | reject | edit"),
    object_value: Optional[str] = Body(default=None),
    asserted_by: Optional[str] = Body(default=None),
    note: Optional[str] = Body(default=None),
    _api_key: str = Depends(require_api_key),
):
    """Owner verdict on one fact: confirm (attest true), reject (close +
    tombstone this value), or edit (correct the value and/or attribution)."""
    from ..features.facts.verdicts import apply_fact_verdict

    try:
        return apply_fact_verdict(
            _entities_conn(),
            object_id=object_id,
            action=action,
            object_value=object_value,
            asserted_by=asserted_by,
            note=note,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/insights")
async def list_stat_insights(
    dimension: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=200, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    """Promoted statistical insights (owner-only rhythms, session stats, trends)."""
    from ..features.facts.reads import list_stat_insights as _list
    from ..features.lifecycle.blackhole_guard import owner_ui_guard

    conn = _entities_conn()
    return _list(conn, guard=owner_ui_guard(conn), dimension=dimension, limit=limit)


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


# --------------------------------------------------------------- timeline rollup

@router.get("/timeline/daily")
async def timeline_daily(
    _api_key: str = Depends(require_api_key),
    days: int = Query(90, ge=1, le=365),
):
    """Per-day lane counts + births + episodes (PLAN_TIMELINE_UNIFIED.md E1)."""
    from ..core.state import get_db_connection
    from ..features.timeline_rollup import timeline_daily_rollup

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return timeline_daily_rollup(conn, days=days)


# --------------------------------------------------------------- attention triage

@router.get("/attention/dashboard")
async def attention_dashboard(
    _api_key: str = Depends(require_api_key),
    days: int = Query(14, ge=1, le=90),
    include_titles: bool = Query(True),
):
    """Data spine for the /data/attention tab (PLAN_ATTENTION_ANALYTICS_EXECUTION WS5.1)."""
    from ..core.state import get_db_connection
    from ..features.triage.dashboard import attention_dashboard_data

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return attention_dashboard_data(conn, days=days, include_titles=include_titles)


# --------------------------------------------------------------- complexity

@router.get("/complexity/summary")
async def complexity_summary(
    _api_key: str = Depends(require_api_key),
    recompute: bool = Query(False),
    weeks: int = Query(12, ge=1, le=104),
    window_days: int = Query(30, ge=1, le=3650),
    half_life_days: Optional[float] = Query(None, ge=0.1, le=3650.0),
):
    """Data spine for the /data/complexity tab (PLAN_COMPLEXITY_DATA_PAGE.md).

    Snapshot computation is CPU-bound (~1s cold on a live-size DB), so it runs
    in a worker thread; warm calls serve the complexity_snapshots cache.
    """
    import asyncio

    from ..core.state import get_db_connection
    from ..features.complexity.engine import get_complexity_summary

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return await asyncio.to_thread(
        get_complexity_summary,
        conn,
        recompute=recompute,
        weeks=weeks,
        window_days=window_days,
        half_life_days=half_life_days,
    )


@router.get("/complexity/timeline")
async def complexity_timeline(
    _api_key: str = Depends(require_api_key),
    recompute: bool = Query(False),
    weeks: int = Query(12, ge=1, le=104),
):
    import asyncio

    from ..core.state import get_db_connection
    from ..features.complexity.engine import get_shift_timeline

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return await asyncio.to_thread(get_shift_timeline, conn, recompute=recompute, weeks=weeks)


@router.get("/complexity/topics/daily")
async def complexity_topics_daily(
    _api_key: str = Depends(require_api_key),
    days: int = Query(90, ge=1, le=365),
    top: int = Query(10, ge=1, le=24),
):
    """Per-day supertopic shares — the braid's substrate (PLAN_TIMELINE_UNIFIED.md E2)."""
    import asyncio

    from ..core.state import get_db_connection
    from ..features.complexity.topics_daily import topics_daily

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return await asyncio.to_thread(topics_daily, conn, days=days, top=top)


@router.get("/complexity/influence")
async def complexity_influence(
    _api_key: str = Depends(require_api_key),
    recompute: bool = Query(False),
    weeks: int = Query(12, ge=1, le=104),
    top_k: int = Query(5, ge=1, le=50),
    window_days: int = Query(90, ge=1, le=3650),
    target: Optional[str] = Query(None, max_length=500),
):
    import asyncio

    from ..core.state import get_db_connection
    from ..features.complexity.engine import get_influence_threads

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return await asyncio.to_thread(
        get_influence_threads,
        conn,
        recompute=recompute,
        weeks=weeks,
        top_k=top_k,
        window_days=window_days,
        target=target,
    )


@router.get("/derivation/packs")
async def get_derivation_packs(_api_key: str = Depends(require_api_key)):
    """Lens catalog (W4.3): registry rows + live fact + quarantine counts."""
    from ..features.derivation.surfaces import list_packs

    return list_packs(_entities_conn())


@router.put("/derivation/packs/{pack_id}")
async def put_derivation_pack(
    pack_id: str,
    enabled: bool = Body(..., embed=True),
    _api_key: str = Depends(require_api_key),
):
    from ..features.derivation.surfaces import set_pack_enabled

    if not set_pack_enabled(_entities_conn(), pack_id, enabled):
        raise HTTPException(status_code=404, detail=f"unknown pack {pack_id}")
    return {"pack_id": pack_id, "enabled": enabled}


@router.get("/facts/conflicts")
async def get_fact_conflicts(
    limit: int = Query(default=100, ge=1, le=500),
    _api_key: str = Depends(require_api_key),
):
    """The A3 quarantine + Tier-2 conflict review queue (W4.2)."""
    from ..features.derivation.surfaces import list_conflicts

    return {"conflicts": list_conflicts(_entities_conn(), limit=limit)}


@router.post("/facts/conflicts/resolve")
async def post_fact_conflict_resolution(
    conflict_id: str = Body(...),
    status: str = Body(..., description="dismissed | accepted"),
    _api_key: str = Depends(require_api_key),
):
    from ..features.derivation.surfaces import resolve_conflict

    try:
        ok = resolve_conflict(_entities_conn(), conflict_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown conflict {conflict_id}")
    return {"conflict_id": conflict_id, "status": status}


@router.post("/derivation/offers/resolve")
async def post_pack_offer_resolution(
    offer_id: str = Body(...),
    action: str = Body(..., description="accept | dismiss"),
    _api_key: str = Depends(require_api_key),
):
    """Self-gating offers: the node offered, the owner decides (W-B)."""
    from ..features.derivation.surfaces import resolve_pack_offer

    try:
        out = resolve_pack_offer(_entities_conn(), offer_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not out:
        raise HTTPException(status_code=404, detail=f"unknown offer {offer_id}")
    return out


@router.post("/derivation/packs/{pack_id}/backfill")
async def post_pack_backfill(
    pack_id: str,
    limit: int = Body(200, embed=True, ge=1, le=1000),
    _api_key: str = Depends(require_api_key),
):
    """Bounded owner-initiated history backfill (lens catalog control)."""
    import asyncio as _asyncio

    from ..features.derivation.surfaces import run_pack_backfill

    try:
        stats = await _asyncio.to_thread(run_pack_backfill, _entities_conn(), pack_id, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"pack_id": pack_id, **stats}
