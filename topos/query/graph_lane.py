"""The graph retrieval lane (S6, PLAN_QUERY_LOOP.md): edges as evidence.

Before this lane, 272 of 38,060 live edges were readable at query time — one
edge type, one cohort resolver. The relations the derivation layer builds
(``communicates_with``, ``co_occurrence``, ``pursues``, ``participates_in``,
``located_at``, ``semantic_affinity``) had no reader on the answer path: the
"Who works on this with me?" probe returned topic-cluster fragments while a
528-degree project hub sat unread one table away.

This lane contributes 1-hop neighborhoods of the query's linked entities as
ordinary fusion candidates — at canonical weight, never above (a lane reached
by a different key but carrying ordinary evidence fuses AT the canonical
weight; see the lane table in retrieval.py).

Privacy posture, deliberately minimal for v1:

* **Owner-only.** The lane runs only at ``disclosure_tier == "owner_raw"`` —
  the same proxy the blackhole summary policy trusts, sound because
  ``resolve_disclosure_tier`` never elevates a grantee there. For any other
  tier the lane returns nothing and writes NO ledger receipt: a receipt for a
  withheld lane is itself an existence signal.
* **Blackhole rides the exit wire.** Items carry the neighbor as
  ``entity_id`` and the anchor as ``subject_entity_id`` — both members of
  ``_BLACKHOLE_ENTITY_ID_KEYS`` — so ``_blackhole_policy_for_summary`` can
  match EITHER endpoint: a black-holed entity can neither appear as a
  neighbor nor be reached through as an anchor. (Owner items are stamped
  ``blackhole_protected``, the taint feed Gate C needs; nothing is silently
  dropped from the owner's own view.)
* **Anchor admission is the thread lane's.** Callers pass anchor ids that
  already went through ``_entity_thread_entities`` — is_self dropped,
  selector policy honored, fail-closed on an unreadable is_self.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..features.entities.edges import top_edges
from . import narrowing as _N
from .overheard import graph_lane_allows_edge_type

logger = logging.getLogger(__name__)

#: Neighbors fetched per anchor before the global cap ranks by weight.
GRAPH_LANE_PER_ANCHOR_LIMIT = 10
#: Total items the lane may contribute to fusion (SUMMARY_ITEM_CAP is 25;
#: a graph that floods the packet would drown the content lanes).
GRAPH_LANE_MAX_ITEMS = 8

_EDGE_SENTENCES: Dict[str, str] = {
    "communicates_with": "{a} communicates with {b}",
    "co_occurrence": "{a} and {b} appear together",
    "pursues": "{a} pursues {b}",
    "participates_in": "{a} participates in {b}",
    "located_at": "{a} is located at {b}",
    "semantic_affinity": "{a} is closely related to {b}",
}


def _edge_sentence(anchor_name: str, neighbor_name: str, edge: Dict[str, Any]) -> str:
    template = _EDGE_SENTENCES.get(
        str(edge.get("edge_type") or ""), "{a} is linked to {b}"
    )
    sentence = template.format(a=anchor_name, b=neighbor_name)
    evidence = edge.get("evidence_count")
    if evidence:
        sentence += f" ({evidence}×)"
    return sentence


def graph_neighborhood_items(
    conn: Any,
    *,
    anchor_ids: List[str],
    anchor_names: Dict[str, str],
    scope_id: str,
    manifest: Any,
    disclosure_tier: str,
    ledger: Optional[Any] = None,
    query_text: str = "",
    plan: Any = None,
) -> List[Dict[str, Any]]:
    """1-hop neighborhoods of the anchors, as fusion-ready summary items.

    Returns [] — with no ledger receipt — unless the tier is owner_raw and
    the scope warrants relationship structure.
    """
    if str(disclosure_tier or "") != "owner_raw":
        return []
    tables = list(getattr(manifest, "canonical_tables", None) or [])
    # relationship_context:read is the deliberate exception: its declared
    # table is conversation_messages, but relationship STRUCTURE is exactly
    # what the scope's own card advertises ("Who works on this with me?") —
    # the same explicit-scope pattern the goals and emotions lanes use.
    if "entity_edges" not in tables and str(scope_id) != "relationship_context:read":
        return []
    if conn is None or not anchor_ids:
        return []

    seen: set = set()
    collected: List[Dict[str, Any]] = []
    for anchor_id in anchor_ids:
        anchor_name = anchor_names.get(anchor_id) or anchor_id
        try:
            edges = top_edges(conn, anchor_id, limit=GRAPH_LANE_PER_ANCHOR_LIMIT)
        except Exception as exc:  # noqa: BLE001 — one bad anchor must not kill the lane
            logger.debug("graph lane: top_edges failed for %s: %s", anchor_id, exc)
            continue
        for edge in edges:
            neighbor_id = str(edge.get("entity_id") or "")
            edge_type = str(edge.get("edge_type") or "")
            if not neighbor_id or not edge_type:
                continue
            if not graph_lane_allows_edge_type(
                edge_type, scope_id=scope_id, query_text=query_text, plan=plan
            ):
                continue
            key = (anchor_id, neighbor_id, edge_type)
            if key in seen:
                continue
            seen.add(key)
            neighbor_name = str(edge.get("entity_name") or neighbor_id)
            event_at = edge.get("last_event_at") or edge.get("valid_from")
            item: Dict[str, Any] = {
                "topic": neighbor_name[:120],
                "summary_text": _edge_sentence(anchor_name, neighbor_name, edge),
                # Neighbor and anchor: both endpoints must be matchable by the
                # blackhole exit wire (_BLACKHOLE_ENTITY_ID_KEYS).
                "entity_id": neighbor_id,
                "subject_entity_id": anchor_id,
                "entity_type": edge.get("entity_type"),
                "edge_type": edge_type,
                "relevance_score": float(edge.get("weight") or 0.0),
                "retrieval_source": f"graph:{edge_type}",
            }
            if event_at:
                item["event_at"] = event_at
            collected.append(item)

    collected.sort(key=lambda i: i.get("relevance_score") or 0.0, reverse=True)
    dropped = max(0, len(collected) - GRAPH_LANE_MAX_ITEMS)
    items = collected[:GRAPH_LANE_MAX_ITEMS]
    if items and ledger is not None:
        try:
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "contributed",
                "graph_lane",
                dropped=dropped,
                detail={
                    "anchors": len(anchor_ids),
                    "contributed": len(items),
                    "edge_types": sorted({i["edge_type"] for i in items}),
                },
            )
        except Exception:  # noqa: BLE001 — the ledger never breaks a turn
            pass
    return items
