"""How much is in the graph — the smallest possible answer.

Why this exists rather than a count taken from ``/v1/signal/graph``: that
endpoint returns node and edge LISTS, capped at 200 and 500, and no totals. To
learn "how many" from it you would fetch rows you throw away, and still not
know the answer once the graph passed the cap.

Why not carry the number on the ingestion job instead: the progress channel is
a fixed set of columns on the control plane's ``ingestion_jobs`` table, with no
JSON field, and ``stage_event`` is filtered down to four keys. Adding it there
means a schema migration on the production database — a real cost to pay for a
reassurance line, and a change that is hard to take back.

So: three COUNT(*) queries, read on demand, owned by nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .registry import handles


def _counts() -> Dict[str, Any]:
    from ...core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        return {"available": False}

    out: Dict[str, Any] = {"available": True}
    # Each table is counted independently and a failure on one is not a failure
    # of the reply: a missing table on an older node should cost that number,
    # not the whole answer.
    for key, table in (
        ("nodes", "graph_nodes"),
        ("edges", "graph_edges"),
        ("entities", "entities"),
    ):
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — fixed names
            out[key] = int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            out[key] = None
    try:
        # The column is `last_run_at` -- what _mark_materialized writes. This read
        # `materialized_at`, which does not exist, and the per-field try/except
        # turned that into a permanent null rather than an error: shipped in
        # 1.3.40 and noticed only because the live reply carried the null.
        row = conn.execute(
            "SELECT MAX(last_run_at) FROM graph_materialization_state"
        ).fetchone()
        out["materialized_at"] = row[0] if row else None
    except Exception:  # noqa: BLE001
        out["materialized_at"] = None
    return out


@handles("graph_summary")
async def handle_graph_summary(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Node, edge and entity counts, for a screen that wants to show growth.

    The reason it is worth showing during an import: entity data lands at the
    third of twenty stages and the graph is materialised twice, so these numbers
    move long before the import finishes. Watching them move is the difference
    between a long wait and a long wait you can see the point of.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        payload = await asyncio.to_thread(_counts)
        return {"id": req_id, "status": "ok", "payload": payload}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
