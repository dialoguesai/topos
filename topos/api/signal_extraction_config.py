"""Read/update device-local signal extraction model overrides (engine_config JSON)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..config.settings import settings
from ..config.signal_extraction import (
    ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
    effective_config_for_api,
    normalize_put_device_overrides,
)
from ..core.state import get_db_connection, set_engine_config_value

logger = logging.getLogger("topos.api.signal_extraction_config")

router = APIRouter(tags=["signal-extraction"])


@router.get("/v1/signal-extraction-config", dependencies=[Depends(require_api_key)])
async def get_signal_extraction_config() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_signal_extraction_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read config") from exc


@router.put("/v1/signal-extraction-config", dependencies=[Depends(require_api_key)])
async def put_signal_extraction_config(body: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        payload = body or {}
        json_str = normalize_put_device_overrides(payload)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE, json_str)
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("put_signal_extraction_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save config") from exc


@router.delete("/v1/signal-extraction-config", dependencies=[Depends(require_api_key)])
async def delete_signal_extraction_device_overrides() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE, "{}")
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete signal_extraction overrides failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to clear config") from exc
