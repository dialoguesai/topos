"""Query-time entity linking: match query text against the entity registry.

Deterministic (alias table + token containment) — no model call, so it can
run on every query. Returns entities ordered by match quality then mention
count.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from .resolver import normalize_name

_MAX_LINKED = 5


def link_query_entities(conn: sqlite3.Connection, query_text: str) -> List[Dict[str, Any]]:
    query_norm = normalize_name(query_text)
    if not query_norm:
        return []
    # Single-character tokens ("a", "i") must never count toward a link —
    # "…for a contact" matching half of "VoxTerm A" is how ghosts get linked.
    query_tokens = {t for t in query_norm.split() if len(t) > 1}
    if not query_tokens:
        return []

    try:
        rows = conn.execute(
            """
            SELECT entity_id, entity_type, canonical_name, normalized_name,
                   aliases_json, mention_count
            FROM entities WHERE mention_count > 0 OR contact_id IS NOT NULL
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    scored: List[Dict[str, Any]] = []
    padded_query = f" {query_norm} "
    for entity_id, etype, name, normalized, aliases_json, mention_count in rows:
        candidates = [str(normalized or "")]
        try:
            candidates += [normalize_name(a) for a in json.loads(aliases_json or "[]")]
        except json.JSONDecodeError:
            pass
        best = 0.0
        for cand in candidates:
            if not cand:
                continue
            if f" {cand} " in padded_query:
                best = max(best, 1.0)  # full name appears in query
                continue
            cand_tokens = {t for t in cand.split() if len(t) > 1}
            if not cand_tokens:
                continue
            overlap = query_tokens & cand_tokens
            if not overlap:
                continue
            # Single-token surnames/short names must match a full token
            best = max(best, len(overlap) / len(cand_tokens))
        if best >= 0.5:
            scored.append(
                {
                    "entity_id": str(entity_id),
                    "entity_type": str(etype),
                    "canonical_name": str(name),
                    "match_score": round(best, 3),
                    "mention_count": int(mention_count or 0),
                }
            )
    scored.sort(key=lambda e: (e["match_score"], e["mention_count"]), reverse=True)
    return scored[:_MAX_LINKED]


def entity_context_items(
    conn: sqlite3.Connection,
    linked: List[Dict[str, Any]],
    *,
    max_per_entity: int = 4,
) -> List[Dict[str, Any]]:
    """Ordered summary items for linked entities: dossier line, then recent mentions."""
    from .dossier import load_dossier_for_entity
    from .edges import top_edges

    items: List[Dict[str, Any]] = []
    for entity in linked:
        entity_id = entity["entity_id"]
        dossier = load_dossier_for_entity(conn, entity_id)
        if dossier:
            text = dossier.get("summary_text") or ""
            connections = dossier.get("top_connections") or []
            if connections:
                text += " Top connections: " + "; ".join(connections[:3])
            for stat_line in dossier.get("stat_lines") or []:
                text += f" {stat_line}"
            items.append(
                {
                    "topic": dossier.get("canonical_name"),
                    "summary_text": text.strip(),
                    "entity_id": entity_id,
                    "retrieval_source": "entity_dossier",
                    "object_type": "entity_dossier",
                    "disclosure": "owner_only",
                }
            )
        else:
            edges = top_edges(conn, entity_id, limit=3)
            if edges:
                text = f"{entity['canonical_name']}: connected to " + ", ".join(
                    e["entity_name"] for e in edges
                )
                items.append(
                    {
                        "topic": entity["canonical_name"],
                        "summary_text": text,
                        "entity_id": entity_id,
                        "retrieval_source": "entity_graph",
                        "object_type": "entity_dossier",
                        "disclosure": "owner_only",
                    }
                )

        mention_rows = conn.execute(
            """
            SELECT record_id, canonical_table, surface_text, event_at, source_id
            FROM entity_mentions WHERE entity_id=?
            ORDER BY COALESCE(event_at, created_at) DESC LIMIT ?
            """,
            (entity_id, max_per_entity),
        ).fetchall()
        for record_id, table, surface, event_at, source_id in mention_rows:
            items.append(
                {
                    "topic": f"{entity['canonical_name']} in {table or 'records'}",
                    "summary_text": f"{str(event_at or '')[:10]} — {surface}",
                    "record_id": record_id,
                    "source_id": source_id,
                    "entity_id": entity_id,
                    "retrieval_source": "entity_mention",
                }
            )
    return items
