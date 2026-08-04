"""openCypher query handler over the entity graph — OWNER-ONLY.

PLAN_GRAPH_QUERY_AND_LATENT_EDGES §4. Cypher is not a new trust class here:
``database_explorer`` already ships arbitrary table browsing and destructive
deletes. It joins that class, and it is registered with the same
``owner_only=True`` marker so the §4.1 leak gate covers it.

The gate is the reason for the marker: an arbitrary traversal punches straight
through row-level redaction, so this type must never reach ``/mcp`` or any
cross-user surface until a scoped-subgraph materialisation story exists.

Query language, limits and worked examples live in
``topos.features.entities.cypher``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .registry import handles


def _payload(message: Dict[str, Any]) -> Dict[str, Any]:
    raw = message.get("payload")
    return raw if isinstance(raw, dict) else {}


@handles("graph_cypher_query", owner_only=True)
async def handle_graph_cypher_query(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = _payload(message)
    try:
        import topos.core.handlers as hub

        from ...features.entities.cypher import DEFAULT_ROW_LIMIT, CypherQueryError, run_cypher

        conn = hub.get_db_connection()
        if conn is None:
            return {"id": req_id, "status": "error", "error": "Database not available", "code": 503}

        try:
            limit = int(payload.get("limit") or DEFAULT_ROW_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_ROW_LIMIT
        try:
            min_weight = float(payload.get("min_weight") or 0.0)
        except (TypeError, ValueError):
            min_weight = 0.0

        try:
            # Materialise-then-match is CPU-bound; keep it off the event loop.
            result = await asyncio.to_thread(
                run_cypher,
                conn,
                str(payload.get("query") or ""),
                limit=limit,
                min_weight=min_weight,
            )
        except CypherQueryError as exc:
            # A malformed query is the caller's problem, not a 5xx.
            return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}
