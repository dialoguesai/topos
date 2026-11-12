"""Owner-only personal-node dashboard; never exposed as an agent tool."""
from .registry import handles


@handles("get_dashboard_measurements", owner_only=True, off_loop=True)
async def get_dashboard_measurements(message):
    from ...core.state import get_db_connection
    from ...dashboard.measurements import daily_measurements

    payload = message.get("payload") or {}
    try:
        result = daily_measurements(get_db_connection(), payload.get("period_start", ""), payload.get("period_end", ""),
                                    time_zone=payload.get("time_zone"),
                                    coherence_window_days=payload.get("coherence_window_days", 30))
        return {"id": message.get("id"), "status": "ok", "payload": result}
    except (ValueError, TypeError):
        return {"id": message.get("id"), "status": "error", "code": 400, "error": "invalid_period"}
