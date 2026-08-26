"""Read/update the owner-selected community-naming model (engine_config).

PLAN_COMMUNITY_NAMING S4: surfaced under Settings → Node functions. Default is
the local extraction model — naming stays on-device unless the owner points it
elsewhere. Empty PUT clears the override.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..core.state import get_db_connection, set_engine_config_value
from ..features.entities.community_naming import (
    ENGINE_CONFIG_KEY_NAMING_MODEL,
    resolve_naming_model,
)

logger = logging.getLogger("topos.api.community_naming_config")

router = APIRouter(tags=["community-naming"])


def _payload(conn) -> dict[str, Any]:
    from ..core.state import get_engine_config_value

    override = get_engine_config_value(conn, ENGINE_CONFIG_KEY_NAMING_MODEL)
    return {
        "model": resolve_naming_model(conn),
        "override": str(override).strip() if override else None,
    }


@router.get("/v1/community-naming-config", dependencies=[Depends(require_api_key)])
async def get_community_naming_config() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    return {"status": "ok", **_payload(conn)}


@router.put("/v1/community-naming-config", dependencies=[Depends(require_api_key)])
async def put_community_naming_config(body: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    model = str((body or {}).get("model") or "").strip()
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_NAMING_MODEL, model or None)
    return {"status": "ok", **_payload(conn)}
