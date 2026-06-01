from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..data_explorer_table_prefs import (
    delete_table_prefs,
    get_table_prefs,
    put_table_prefs,
)

router = APIRouter(tags=["data-explorer-table-prefs"])


def _map_validation_error(err: ValueError) -> HTTPException:
    code = str(err)
    mapping = {
        "INVALID_PREFS": (status.HTTP_400_BAD_REQUEST, "Invalid table preferences payload."),
        "INVALID_SORT": (status.HTTP_400_BAD_REQUEST, "Invalid sort state."),
        "INVALID_TABLE_NAME": (status.HTTP_400_BAD_REQUEST, "Invalid table name."),
        "INVALID_USER_ID": (status.HTTP_400_BAD_REQUEST, "Invalid user id."),
        "PREFS_TOO_LARGE": (status.HTTP_400_BAD_REQUEST, "Table preferences exceed maximum size."),
    }
    status_code, message = mapping.get(code, (status.HTTP_400_BAD_REQUEST, "Invalid request."))
    return HTTPException(status_code=status_code, detail={"error": message, "code": code})


@router.get("/v1/data-explorer/table-prefs/{table_name}", dependencies=[Depends(require_api_key)])
async def read_data_explorer_table_prefs(
    table_name: str,
    user_id: str = Query(..., min_length=1),
) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    try:
        prefs = get_table_prefs(conn, user_id=user_id, table_name=table_name)
    except ValueError as exc:
        raise _map_validation_error(exc) from exc
    return {"status": "ok", "prefs": prefs}


@router.put("/v1/data-explorer/table-prefs/{table_name}", dependencies=[Depends(require_api_key)])
async def write_data_explorer_table_prefs(
    table_name: str,
    body: dict[str, Any] = Body(default=None),
) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    payload = body or {}
    user_id = str(payload.get("user_id") or "").strip()
    prefs = payload.get("prefs")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "user_id is required", "code": "INVALID_USER_ID"},
        )
    try:
        saved = put_table_prefs(conn, user_id=user_id, table_name=table_name, prefs=prefs or {})
    except ValueError as exc:
        raise _map_validation_error(exc) from exc
    return {"status": "ok", "prefs": saved}


@router.delete("/v1/data-explorer/table-prefs/{table_name}", dependencies=[Depends(require_api_key)])
async def remove_data_explorer_table_prefs(
    table_name: str,
    user_id: str = Query(..., min_length=1),
) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    try:
        deleted = delete_table_prefs(conn, user_id=user_id, table_name=table_name)
    except ValueError as exc:
        raise _map_validation_error(exc) from exc
    return {"status": "ok", "deleted": deleted}
