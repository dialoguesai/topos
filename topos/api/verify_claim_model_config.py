"""Read/update the owner's verify_claim model (engine_config), the HTTP twin of the ``*_verify_claim_model_config``
message types. Same shape and doors as ``/v1/facts-llm-config``: the read is open on the owner socket, the write and
the clear are owner-confirmed changes (``owner_door_routes``)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..config.settings import settings
from ..config.verify_claim_model import NO_PICK, effective_config_for_api, normalize_put_config, save_pick
from ..core.state import get_db_connection

logger = logging.getLogger("topos.api.verify_claim_model_config")

router = APIRouter(tags=["verify-claim"])


@router.get("/v1/verify-claim-model-config", dependencies=[Depends(require_api_key)])
async def get_verify_claim_model_config() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_verify_claim_model_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read config") from exc


@router.put("/v1/verify-claim-model-config", dependencies=[Depends(require_api_key)])
async def put_verify_claim_model_config(body: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        save_pick(conn, normalize_put_config(body))
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("put_verify_claim_model_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save config") from exc


@router.delete("/v1/verify-claim-model-config", dependencies=[Depends(require_api_key)])
async def delete_verify_claim_model_config() -> dict[str, Any]:
    """Clear the pick; verify_claim goes back to the environment's choice, then the key-present default."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        save_pick(conn, NO_PICK)
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_verify_claim_model_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to clear config") from exc
