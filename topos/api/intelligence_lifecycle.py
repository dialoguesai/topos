"""Owner data-control API: exclude artifacts from derived intelligence.

These endpoints are the "remove things I don't want included" surface:
  GET    /intelligence/exclusions            — list tombstones
  POST   /intelligence/exclusions/fact       — soft-close + tombstone a fact
  POST   /intelligence/exclusions/entity     — stop tracking an entity
  POST   /intelligence/exclusions/stat       — suppress a stat insight
  POST   /intelligence/exclusions/record     — purge one record's derived traces
  DELETE /intelligence/exclusions            — lift a tombstone (re-inclusion
                                               happens on the next enrichment)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..core.state import get_db_connection

router = APIRouter(tags=["intelligence-lifecycle"])


def _store():
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="Database not available")
    from ..features.lifecycle.exclusions import ExclusionStore

    return ExclusionStore(conn)


@router.get("/intelligence/exclusions", dependencies=[Depends(require_api_key)])
async def list_exclusions(artifact_type: Optional[str] = None) -> Dict[str, Any]:
    items = _store().list(artifact_type=artifact_type)
    return {"items": items, "total": len(items)}


@router.post("/intelligence/exclusions/fact", dependencies=[Depends(require_api_key)])
async def exclude_fact(
    subject_entity_id: str = Body(...),
    predicate: str = Body(...),
    object_value: Optional[str] = Body(None),
    note: Optional[str] = Body(None),
) -> Dict[str, Any]:
    try:
        return _store().exclude_fact(
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            object_value=object_value,
            note=note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/intelligence/exclusions/entity", dependencies=[Depends(require_api_key)])
async def exclude_entity(
    entity_ref: str = Body(..., description="entity_id or name"),
    note: Optional[str] = Body(None),
) -> Dict[str, Any]:
    try:
        return _store().exclude_entity(entity_ref=entity_ref, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/intelligence/exclusions/stat", dependencies=[Depends(require_api_key)])
async def exclude_stat_insight(
    stat_id: str = Body(...),
    group_key: str = Body(""),
    note: Optional[str] = Body(None),
) -> Dict[str, Any]:
    try:
        return _store().exclude_stat_insight(stat_id=stat_id, group_key=group_key, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/intelligence/exclusions/record", dependencies=[Depends(require_api_key)])
async def exclude_record(
    record_id: str = Body(...),
    note: Optional[str] = Body(None),
) -> Dict[str, Any]:
    try:
        return _store().exclude_record(record_id=record_id, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/intelligence/exclusions", dependencies=[Depends(require_api_key)])
async def remove_exclusion(
    artifact_type: str = Body(...),
    artifact_key: str = Body(...),
) -> Dict[str, Any]:
    removed = _store().remove_exclusion(artifact_type, artifact_key)
    return {"removed": removed}
