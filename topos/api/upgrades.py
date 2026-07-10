"""Upgrade status surface: what the runner + graph refresher are doing.

Read-only. The UI shows "Upgrading your intelligence…" instead of a
mysteriously thin graph while re-derivation runs.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..auth import require_api_key

router = APIRouter()


def _status_payload() -> Dict[str, Any]:
    from ..core.state import get_db_connection
    from ..features.entities.graph_refresh import status as refresh_status
    from ..upgrades.runner import runner_status

    conn = get_db_connection()
    upgrade = runner_status(conn) if conn is not None else {"enabled": False, "error": "no database"}
    return {"upgrade": upgrade, "graph_refresh": refresh_status()}


@router.get("/v1/upgrade/status", dependencies=[Depends(require_api_key)])
async def get_upgrade_status() -> Dict[str, Any]:
    return _status_payload()
