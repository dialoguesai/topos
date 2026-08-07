"""Upgrade status + consent surface for schema/derived migration steps.

The UI shows "Upgrading your intelligence…" while auto steps run, and prompts
for ``pending_consent`` slow-lane steps (PLAN_NODE_RELEASE_MIGRATIONS M3).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_api_key

router = APIRouter()


def _status_payload() -> Dict[str, Any]:
    from ..core.state import get_db_connection
    from ..features.entities.graph_refresh import status as refresh_status
    from ..sanitization.prewarm import prewarm_status
    from ..upgrades.runner import runner_status

    conn = get_db_connection()
    upgrade = runner_status(conn) if conn is not None else {"enabled": False, "error": "no database"}
    # `models` rides this existing read because the control plane already
    # proxies it verbatim (engine_config.py returns `result.get("payload")`),
    # `get_upgrade_status` is already allowlisted, and it is in the node's
    # fast-inbound set — so it answers in milliseconds even while the prewarm
    # thread is saturated downloading. No control-plane change, no CP deploy.
    return {"upgrade": upgrade, "graph_refresh": refresh_status(), "models": prewarm_status()}


@router.get("/v1/upgrade/status", dependencies=[Depends(require_api_key)])
async def get_upgrade_status() -> Dict[str, Any]:
    return _status_payload()


class UpgradeConsentBody(BaseModel):
    step_id: str = Field(..., min_length=1)
    # Optional: run the step immediately after consent (default true).
    run_now: bool = True


@router.post("/v1/upgrade/consent", dependencies=[Depends(require_api_key)])
async def post_upgrade_consent(body: UpgradeConsentBody) -> Dict[str, Any]:
    from ..core.state import get_db_connection
    from ..upgrades.runner import consent_upgrade_step, run_pending_upgrades

    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    try:
        result = consent_upgrade_step(conn, body.step_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run_result: Optional[Dict[str, Any]] = None
    if body.run_now:
        run_result = run_pending_upgrades(conn)
    return {"status": "ok", "consent": result, "run": run_result, **_status_payload()}
