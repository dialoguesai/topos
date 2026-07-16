"""Structured signal extraction: artifacts first, briefs for narrative dimensions only."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ...core.state import get_db_connection
from .dimension_definition_loader import view_types_for_dimension
from .dimension_registry import dimensions_for_brief_update
from .extraction.artifact_router import route_canonical_batch
from .signal_object_store import SignalObjectStore
from .typed_stores.aggregates import recompute_all_gate_aggregates
from ...sources.registry import REGISTRY


_STRUCTURED_VIEWS = frozenset(
    {"interval_calendar", "relationship_graph", "goal_list", "skill_tags", "project_summary"}
)
_NARRATIVE_BRIEF_OPTIONAL = "narrative_brief_optional"


def structured_signal_enabled() -> bool:
    return os.environ.get("TOPOS_STRUCTURED_SIGNAL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def is_narrative_primary_dimension(dimension: str) -> bool:
    """True when the dimension has no structured view types (prose brief is the primary surface)."""
    views = set(view_types_for_dimension(dimension))
    return not views.intersection(_STRUCTURED_VIEWS)


def should_update_narrative_brief(dimension: str) -> bool:
    """True when dimension_summary should run an LLM brief merge for this dimension."""
    views = set(view_types_for_dimension(dimension))
    if _NARRATIVE_BRIEF_OPTIONAL in views:
        return True
    return is_narrative_primary_dimension(dimension)


def enrich_structured_batch(
    prepared_records: List[Dict[str, Any]],
    *,
    source_id: Optional[str] = None,
    only_dimension: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"ok": False, "error": "database_unavailable"}

    routed_records: List[Dict[str, Any]] = []
    for rec in prepared_records:
        row = dict(rec)
        sid = str(row.get("source_id") or source_id or "")
        defn = REGISTRY.get(sid)
        table = str(row.get("canonical_table") or "")
        if not table and defn is not None:
            group = str(getattr(defn, "canonical_group_id", "") or "")
            table = _table_for_group(group, row)
        if table:
            row["canonical_table"] = table
            routed_records.append(row)

    totals = route_canonical_batch(conn, routed_records)
    aggregates = recompute_all_gate_aggregates(SignalObjectStore(conn))
    from .typed_stores.scope_materializer import materialize_scope_signal_objects

    materialized = materialize_scope_signal_objects(conn)

    dimensions = dimensions_for_brief_update(
        source_id=str(source_id or ""),
        only_dimension=only_dimension,
    )
    narrative_dims = [d for d in dimensions if should_update_narrative_brief(d)]

    return {
        "ok": True,
        "artifacts": totals["artifacts"],
        "objects": totals["objects"],
        "aggregates": aggregates,
        "materialized": materialized,
        "narrative_dimensions": narrative_dims,
    }


def _table_for_group(group: str, row: Dict[str, Any]) -> str:
    mapping = {
        "schedule": "calendar_events",
        "contacts": "contacts",
        "conversations": "conversation_messages",
        "profile": "profile_records",
        "ai_messages": "ai_chat_messages",
        "journal": "journal_entries",
        "financial": "financial_transactions",
        "places": "location_events",
        "activity": "activity_events",
        # documents is deliberately NOT mapped here: it is an owner-only leaf
        # lane (store-and-view only) that must never feed derived/shared signals.
    }
    if group in mapping:
        return mapping[group]
    if row.get("event_id") or row.get("starts_at"):
        return "calendar_events"
    if row.get("message_id") or row.get("conversation_id"):
        return "conversation_messages"
    if row.get("contact_id"):
        return "contacts"
    if row.get("record_type"):
        return "profile_records"
    return ""
