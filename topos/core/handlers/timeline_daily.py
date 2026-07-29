"""Timeline daily-rollup handler (PLAN_TIMELINE_UNIFIED.md E1).

Own module (not signal_features.py) so the rollup lane stays independently
reviewable. Reads only; served sync — the grouped scans are index-friendly
and small.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import handles


def _clamped_days(payload: Dict[str, Any]) -> int:
    try:
        value = int(payload.get("days") or 90)
    except (TypeError, ValueError):
        return 90
    return max(1, min(365, value))


@handles("signal_timeline_daily")
async def handle_signal_timeline_daily(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        import topos.core.handlers as hub

        from ...features.timeline_rollup import timeline_daily_rollup

        conn = hub.get_db_connection()
        if conn is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}
        result = timeline_daily_rollup(conn, days=_clamped_days(payload or {}))
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}
