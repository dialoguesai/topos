"""Complexity analytics handlers (focus/structure readings + influence threads).

The snapshot computation is CPU-bound (seconds on a live-size DB); it runs on
a worker thread through ``run_db_read`` so the node's event loop keeps serving
other messages. The worker resolves its OWN connection there (one Connection,
one thread — see ``topos.core.state.get_db_connection``); all commits go
through the process-wide write gate (topos.features.complexity.store).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .common import run_db_read
from .registry import handles


def _payload(message: Dict[str, Any]) -> Dict[str, Any]:
    raw = message.get("payload")
    return raw if isinstance(raw, dict) else {}


def _clamped_int(payload: Dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(payload.get(key) or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _clamped_float(
    payload: Dict[str, Any], key: str, lo: float, hi: float
) -> Optional[float]:
    if not payload.get(key):
        return None
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, value))


@handles("complexity_summary")
async def handle_complexity_summary(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = _payload(message)
    try:
        import topos.core.handlers as hub

        from ...features.complexity.engine import get_complexity_summary

        if hub.get_db_connection() is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}
        result = await run_db_read(
            get_complexity_summary,
            recompute=bool(payload.get("recompute")),
            weeks=_clamped_int(payload, "weeks", 12, 1, 104),
            window_days=_clamped_int(payload, "window_days", 30, 1, 3650),
            half_life_days=_clamped_float(payload, "half_life_days", 0.1, 3650.0),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}


@handles("complexity_timeline")
async def handle_complexity_timeline(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = _payload(message)
    try:
        import topos.core.handlers as hub

        from ...features.complexity.engine import get_shift_timeline

        if hub.get_db_connection() is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}
        result = await run_db_read(
            get_shift_timeline,
            recompute=bool(payload.get("recompute")),
            weeks=_clamped_int(payload, "weeks", 12, 1, 104),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}


@handles("complexity_topics_daily")
async def handle_complexity_topics_daily(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = _payload(message)
    try:
        import topos.core.handlers as hub

        from ...features.complexity.topics_daily import topics_daily

        if hub.get_db_connection() is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}
        result = await run_db_read(
            topics_daily,
            days=_clamped_int(payload, "days", 90, 1, 365),
            top=_clamped_int(payload, "top", 10, 1, 24),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}


@handles("complexity_influence")
async def handle_complexity_influence(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = _payload(message)
    try:
        import topos.core.handlers as hub

        from ...features.complexity.engine import get_influence_threads

        if hub.get_db_connection() is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}
        result = await run_db_read(
            get_influence_threads,
            recompute=bool(payload.get("recompute")),
            weeks=_clamped_int(payload, "weeks", 12, 1, 104),
            top_k=_clamped_int(payload, "top_k", 5, 1, 50),
            window_days=_clamped_int(payload, "window_days", 90, 1, 3650),
            target=(str(payload["target"])[:500] if payload.get("target") else None),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}
