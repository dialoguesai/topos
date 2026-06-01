# Topos UMA RPT validation: call Control Plane introspect_for_resource
# US-3.7: RPT validation for topos.cli (engine deprecated)
# See: control_plane/uma/uma_sprints/SPRINT_3_USER_SHARING.md

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .config.settings import settings

logger = logging.getLogger("topos.uma_rpt")


def get_control_plane_http_base() -> Optional[str]:
    """Control Plane HTTP base URL for REST (introspect). Derives from TOPOS_CONTROL_PLANE_URL if ws/wss."""
    base = settings.topos_control_plane_url
    if not base:
        return None
    base = base.strip()
    if base.startswith("wss://"):
        base = base.replace("wss://", "https://", 1)
    elif base.startswith("ws://"):
        base = base.replace("ws://", "http://", 1)
    if "/ws/" in base:
        base = base.split("/ws/")[0]
    return base.rstrip("/") or None


async def introspect_for_resource(
    resource_id: str,
    token: str,
    *,
    control_plane_base: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Call Control Plane POST /v1/uma/introspect_for_resource. Returns enriched
    introspection (allowed_scopes, filters) or raises.
    """
    base = control_plane_base or get_control_plane_http_base()
    if not base:
        raise ValueError("Control Plane URL not configured; set TOPOS_CONTROL_PLANE_URL")
    url = f"{base}/v1/uma/introspect_for_resource"
    payload = {"token": token, "resource_id": resource_id}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code == 401:
        raise RPTValidationError("Token inactive or expired", status_code=401)
    if resp.status_code == 403:
        raise RPTValidationError("RPT does not grant access to this resource", status_code=403)
    if resp.status_code == 404:
        raise RPTValidationError("Resource not found", status_code=404)
    if resp.status_code >= 400:
        raise RPTValidationError(
            resp.text or f"Introspection failed: {resp.status_code}",
            status_code=resp.status_code,
        )
    return resp.json()


class RPTValidationError(Exception):
    """RPT validation failed (introspect_for_resource returned error)."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code
