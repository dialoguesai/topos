from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Query

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..storage.user_identity import get_user_identity, put_user_identity

router = APIRouter(tags=["user-identity"])


@router.get("/v1/user-identity", dependencies=[Depends(require_api_key)])
async def get_user_identity_endpoint(
    dataset_id: Optional[str] = Query(default=None, description="Dataset scope for owner identity"),
):
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    identity = get_user_identity(conn, dataset_id)
    if identity is None:
        return {"status": "ok", "dataset_id": dataset_id, "display_name": None}
    return {"status": "ok", "dataset_id": dataset_id, **identity}


@router.put("/v1/user-identity", dependencies=[Depends(require_api_key)])
async def put_user_identity_endpoint(
    dataset_id: Optional[str] = Query(default=None),
    display_name: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    if not dataset_id and body:
        dataset_id = body.get("dataset_id")
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    next_display_name = display_name if display_name is not None else (body.get("display_name") if body else None)
    if isinstance(next_display_name, str):
        next_display_name = next_display_name.strip() or None
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    put_user_identity(conn, dataset_id, display_name=next_display_name)
    return {"status": "ok", "dataset_id": dataset_id, "display_name": next_display_name}
