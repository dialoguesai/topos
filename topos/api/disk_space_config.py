"""Read/update the owner's minimum-free-disk floor, and report where we stand.

The floor is one number in `engine_config` (`min_free_disk_bytes`, default
10 GB). Everything else here is derived from it: the disk check refuses pulls
that would cross it, and the model manager evicts re-downloadable models to
climb back above it.

Two endpoints rather than one because they answer different questions at
different rates. The policy is a setting the owner edits; the status is a
measurement the app polls. Folding them together would either make the settings
form re-probe the volume on every render or make the sidebar warning wait on a
write path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..config.settings import (
    ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES,
    MIN_FREE_DISK_BYTES_DEFAULT,
    MIN_FREE_DISK_BYTES_MAX,
    MIN_FREE_DISK_BYTES_MIN,
    resolve_min_free_disk_bytes,
    settings,
)
from ..core.state import get_db_connection, set_engine_config_value

logger = logging.getLogger("topos.api.disk_space_config")

router = APIRouter(tags=["disk-space"])


def policy_payload(conn: Any) -> Dict[str, Any]:
    """The floor plus the bounds the UI needs to build its input."""
    return {
        "min_free_bytes": resolve_min_free_disk_bytes(settings, conn),
        "default_min_free_bytes": MIN_FREE_DISK_BYTES_DEFAULT,
        "min_allowed_bytes": MIN_FREE_DISK_BYTES_MIN,
        "max_allowed_bytes": MIN_FREE_DISK_BYTES_MAX,
    }


def normalize_put_min_free_bytes(payload: Any) -> int:
    """The requested floor in bytes, or ValueError naming what was wrong.

    Rejects rather than clamps, unlike the resolver. A stored value out of range
    is a fact we have to live with and degrade from; a value arriving from the
    settings form is a request we can answer honestly — silently saving 1 TB
    when the owner typed 900 TB would leave the form showing a number the node
    never agreed to.
    """
    body = payload if isinstance(payload, dict) else {}
    if "min_free_bytes" not in body:
        raise ValueError("min_free_bytes is required")
    raw = body.get("min_free_bytes")
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_free_bytes must be a number of bytes") from exc
    if value < MIN_FREE_DISK_BYTES_MIN or value > MIN_FREE_DISK_BYTES_MAX:
        raise ValueError(
            f"min_free_bytes must be between {MIN_FREE_DISK_BYTES_MIN} and "
            f"{MIN_FREE_DISK_BYTES_MAX} bytes"
        )
    return value


def _status_sync(conn: Any) -> Dict[str, Any]:
    from ..engine.model_manager import status as model_manager_status

    return {**model_manager_status(conn), **policy_payload(conn)}


async def get_disk_status_core(conn: Any = None) -> Dict[str, Any]:
    """Disk floor, free space, and what eviction could still free.

    Off the event loop: it stats a volume and asks Ollama for its tag list, and
    a slow or wedged daemon must not stall every other request on this node.
    """
    resolved = conn if conn is not None else get_db_connection()
    return await asyncio.to_thread(_status_sync, resolved)


@router.get("/v1/disk-space-policy", dependencies=[Depends(require_api_key)])
async def get_disk_space_policy() -> Dict[str, Any]:
    return {"status": "ok", **policy_payload(get_db_connection())}


@router.put("/v1/disk-space-policy", dependencies=[Depends(require_api_key)])
async def put_disk_space_policy(body: Dict[str, Any] = Body(default=None)) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        value = normalize_put_min_free_bytes(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES, str(value))
    except Exception as exc:  # noqa: BLE001
        logger.warning("put_disk_space_policy failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save disk floor") from exc
    return {"status": "ok", **policy_payload(conn)}


@router.get("/v1/disk-status", dependencies=[Depends(require_api_key)])
async def get_disk_status() -> Dict[str, Any]:
    return {"status": "ok", **(await get_disk_status_core())}
