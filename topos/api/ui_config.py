from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends

from ..auth import require_api_key
from ..core.state import get_db_connection, get_engine_config_value, set_engine_config_value

router = APIRouter(tags=["ui-config"])

UI_CONFIG_KEY = "ui_config"
ALLOWED_WIDGETS = {
    "umaAllTime",
    "uma24h",
    "mcpRows",
    "mcp24h",
    "topConnector",
    "umaStatusMix",
    "topConnectors",
    "mcpSources",
}
MAX_PINNED = 3


GRAPH_WINDOW_MAX_DAYS = 3650


def _default_ui_config() -> dict[str, Any]:
    return {"version": 1, "topbar": {"pinnedAnalytics": []}, "graph": {}}


def _normalize_ui_config(value: Any) -> dict[str, Any]:
    base = _default_ui_config()
    if not isinstance(value, dict):
        return base
    out = {
        "version": int(value.get("version", 1)) if str(value.get("version", "")).isdigit() else 1,
        "topbar": {"pinnedAnalytics": []},
        "graph": {},
    }
    # Graph view prefs: the temporal window is personal (data volume/velocity
    # dependent — 2 days for some owners, 2 months for others). timeWindowDays
    # is a whole-day count; explicit null = all time; absent = unset.
    # trackLookbackDays caps how far left the scrubber track extends.
    graph = value.get("graph")
    if isinstance(graph, dict) and "timeWindowDays" in graph:
        window = graph.get("timeWindowDays")
        if window is None:
            out["graph"]["timeWindowDays"] = None
        elif isinstance(window, (int, float)) and not isinstance(window, bool):
            days = int(window)
            if 1 <= days <= GRAPH_WINDOW_MAX_DAYS:
                out["graph"]["timeWindowDays"] = days
    if isinstance(graph, dict) and "trackLookbackDays" in graph:
        lookback = graph.get("trackLookbackDays")
        if lookback is None:
            out["graph"]["trackLookbackDays"] = None
        elif isinstance(lookback, (int, float)) and not isinstance(lookback, bool):
            days = int(lookback)
            if 1 <= days <= GRAPH_WINDOW_MAX_DAYS:
                out["graph"]["trackLookbackDays"] = days
    if isinstance(graph, dict):
        mode = graph.get("nodeColorMode")
        if mode in ("type", "community"):
            out["graph"]["nodeColorMode"] = mode
    topbar = value.get("topbar")
    pinned = topbar.get("pinnedAnalytics") if isinstance(topbar, dict) else []
    if not isinstance(pinned, list):
        return out
    seen: set[str] = set()
    result: list[str] = []
    for item in pinned:
        wid = str(item or "").strip()
        if not wid or wid in seen or wid not in ALLOWED_WIDGETS:
            continue
        seen.add(wid)
        result.append(wid)
        if len(result) >= MAX_PINNED:
            break
    out["topbar"]["pinnedAnalytics"] = result
    return out


@router.get("/v1/ui-config", dependencies=[Depends(require_api_key)])
async def get_ui_config() -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    raw = get_engine_config_value(conn, UI_CONFIG_KEY)
    if not raw:
        cfg = _default_ui_config()
        return {"status": "ok", "ui_config": cfg}
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    cfg = _normalize_ui_config(parsed)
    return {"status": "ok", "ui_config": cfg}


@router.put("/v1/ui-config", dependencies=[Depends(require_api_key)])
async def put_ui_config(body: dict[str, Any] = Body(default=None)) -> dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    payload = (body or {}).get("ui_config")
    cfg = _normalize_ui_config(payload)
    set_engine_config_value(conn, UI_CONFIG_KEY, json.dumps(cfg))
    return {"status": "ok", "ui_config": cfg}

