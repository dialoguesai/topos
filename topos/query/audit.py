"""Query audit event builder (PRD §8.8)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .session import TurnOutcome


def build_query_audit_event(
    *,
    turn_outcome: TurnOutcome | str,
    scope_id: str,
    access_mode: str,
    session_id: str,
    game_layer_strategy: str = "direct",
    stores_touched: Optional[List[str]] = None,
    filters_applied: Optional[List[str]] = None,
    cache_keys: Optional[List[str]] = None,
    deny_reason: Optional[str] = None,
    retrieval_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outcome = turn_outcome.value if isinstance(turn_outcome, TurnOutcome) else str(turn_outcome)
    event = {
        "turn_outcome": outcome,
        "scope_id": scope_id,
        "access_mode": access_mode,
        "session_id": session_id,
        "game_layer_strategy": game_layer_strategy,
        "stores_touched": list(stores_touched or []),
        "filters_applied": list(filters_applied or []),
        "cache_keys": list(cache_keys or []),
    }
    if deny_reason:
        event["deny_reason"] = deny_reason
    if retrieval_metadata:
        for key in ("retrieval_strategy", "vector_hits", "clusters_returned", "canonical_source"):
            if key in retrieval_metadata:
                event[key] = retrieval_metadata[key]
    if outcome == TurnOutcome.MEMORY_HIT.value:
        event["stores_touched"] = []
        event["retrieval_skipped"] = True
    else:
        event["retrieval_skipped"] = False
    return event
