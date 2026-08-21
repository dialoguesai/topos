"""Read/update the owner-selected facts-LLM extraction model (engine_config)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..config.facts_llm import (
    ENGINE_CONFIG_KEY_FACTS_LLM_MODEL,
    ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER,
    effective_config_for_api,
    normalize_put_config,
)
from ..config.settings import settings
from ..core.state import get_db_connection, set_engine_config_value

logger = logging.getLogger("topos.api.facts_llm_config")

router = APIRouter(tags=["facts-llm"])


@router.get("/v1/facts-llm-config", dependencies=[Depends(require_api_key)])
async def get_facts_llm_config() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_facts_llm_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to read config") from exc


@router.put("/v1/facts-llm-config", dependencies=[Depends(require_api_key)])
async def put_facts_llm_config(body: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        provider, model = normalize_put_config(body)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, model)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, provider)
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("put_facts_llm_config failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save config") from exc


@router.delete("/v1/facts-llm-config", dependencies=[Depends(require_api_key)])
async def delete_facts_llm_model_override() -> dict[str, Any]:
    """Clear the device override; the effective model reverts to env defaults."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, "")
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, "")
        return {"status": "ok", **effective_config_for_api(settings, conn)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete facts_llm model override failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to clear config") from exc
