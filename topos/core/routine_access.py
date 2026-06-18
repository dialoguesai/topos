"""Access attribution constants for Agents / scheduled routines."""

from __future__ import annotations

from typing import Any, Dict, Optional

ROUTINE_MCP_SOURCE = "routine_executor"
ROUTINE_APP_ID = "topos-routines"
ROUTINE_ACCESS_CONTEXT = "routine"
ROUTINE_ACCESS_CHANNEL = "routine"


def is_routine_mcp_source(source: Optional[str]) -> bool:
    return str(source or "").strip().lower() == ROUTINE_MCP_SOURCE


def routine_uma_attribution(*, mcp_source: Optional[str]) -> Dict[str, Any]:
    """UMA row fields when access was initiated by a routine run."""
    if not is_routine_mcp_source(mcp_source):
        return {}
    return {
        "app_id": ROUTINE_APP_ID,
        "access_channel": ROUTINE_ACCESS_CHANNEL,
        "access_context": ROUTINE_ACCESS_CONTEXT,
    }
