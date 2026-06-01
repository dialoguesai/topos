from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

from ..config.settings import settings

logger = logging.getLogger("topos.engine.usage_guard")

_KNOWN_GUARD_REASON_CODES = {
    "LIMIT_EXCEEDED",
    "MISSING_POLICY_RULE",
    "UNTRUSTED_USAGE_SOURCE",
    "METRIC_NOT_REGISTERED",
    "PERIOD_RESOLUTION_FALLBACK",
}


def parse_guard_denial_payload(detail: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(detail, dict):
        return None
    reason_code = str(detail.get("reason_code") or "").strip()
    metric_key = str(detail.get("metric_key") or "").strip()
    period_start = str(detail.get("period_start") or "").strip()
    period_end = str(detail.get("period_end") or "").strip()
    if not reason_code or reason_code not in _KNOWN_GUARD_REASON_CODES:
        return None
    return {
        "reason_code": reason_code,
        "metric_key": metric_key or None,
        "period_start": period_start or None,
        "period_end": period_end or None,
        "used": detail.get("used"),
        "limit": detail.get("limit"),
        "unlimited": detail.get("unlimited"),
        "policy_version": detail.get("policy_version"),
        "message": str(detail.get("message") or "Usage request denied by guard."),
    }


def classify_guard_submission_error(status_code: int, detail: Any) -> str:
    denial = parse_guard_denial_payload(detail)
    if status_code == 402 and denial is not None:
        return "guard_denial"
    return "transport_or_system_error"


async def submit_usage_guard_check(
    *,
    usage_kind: str,
    units: int,
    timeout_seconds: float = 10.0,
) -> Dict[str, Any]:
    if os.getenv("TOPOS_USAGE_GUARD_ENFORCEMENT_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"allowed": True, "source": "guard_not_enabled"}

    control_plane_http = str(settings.topos_control_plane_url or "").strip()
    api_key = str(settings.topos_key or "").strip()
    if control_plane_http.startswith("wss://"):
        control_plane_http = control_plane_http.replace("wss://", "https://")
    elif control_plane_http.startswith("ws://"):
        control_plane_http = control_plane_http.replace("ws://", "http://")
    control_plane_http = control_plane_http.split("/ws/")[0].rstrip("/")

    # Allow dev/test operation when control-plane connectivity is absent.
    if not control_plane_http or not api_key:
        return {"allowed": True, "source": "guard_not_configured"}

    url = f"{control_plane_http}/v1/billing/usage/charge"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {"usage_kind": usage_kind, "units": int(max(0, units))}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        body = response.json() if response.content else {}
        return {"allowed": True, "source": "control_plane", "payload": body}

    detail = None
    try:
        body = response.json() if response.content else {}
        detail = body.get("detail", body)
    except Exception:
        detail = response.text
    classification = classify_guard_submission_error(response.status_code, detail)
    if classification == "guard_denial":
        denial = parse_guard_denial_payload(detail) or {}
        logger.info(
            "usage guard denied metric=%s reason=%s period_start=%s period_end=%s",
            denial.get("metric_key"),
            denial.get("reason_code"),
            denial.get("period_start"),
            denial.get("period_end"),
        )
        return {"allowed": False, "source": "control_plane", "denial": denial}
    logger.warning("usage guard transport/system failure status=%s", response.status_code)
    response.raise_for_status()
    return {"allowed": True, "source": "control_plane_unreachable"}
