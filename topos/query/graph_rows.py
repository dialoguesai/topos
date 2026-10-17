"""Owner graph reads over the entity spine, rather than the canonical adapter.

Entities and entity_edges are derived tables: canonical.list() cannot read
them. Keep this lane owner-only until graph edges carry grant-safe lineage.
"""
from __future__ import annotations

from typing import Any


def graph_rows(conn: Any, table: str, query: str, *, limit: int,
               disclosure_tier: str) -> list[dict]:
    if conn is None or disclosure_tier != "owner_raw" or table not in ("entities", "entity_edges"):
        return []
    from ..features.entities.linking import link_query_entities
    from ..features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass

    # The query pipeline has already resolved the tier from the authenticated
    # principal. Owner agents still receive the ordinary blackhole guard.
    guard = BlackholeGuard(conn, caller_class=CallerClass.OWNER_AGENT)
    anchors = [r["entity_id"] for r in link_query_entities(conn, query)] if query.strip() else []
    if query.strip() and not anchors:
        return []  # a specific unmatched ask must not become a graph dump
    params: list[Any] = []
    if table == "entities":
        sql = "SELECT entity_id AS record_id, entity_id, entity_type, canonical_name, mention_count, last_seen AS event_at FROM entities"
        if anchors:
            sql += " WHERE entity_id IN (" + ",".join("?" for _ in anchors) + ")"
            params.extend(anchors)
        sql += " ORDER BY mention_count DESC, entity_id LIMIT ?"
    else:
        sql = """SELECT e.edge_id AS record_id, e.edge_id,
                 e.src_entity_id, e.dst_entity_id, e.edge_type, e.weight,
                 e.evidence_count, e.last_event_at AS event_at,
                 s.canonical_name AS source_name, d.canonical_name AS target_name
                 FROM entity_edges e JOIN entities s ON s.entity_id=e.src_entity_id
                 JOIN entities d ON d.entity_id=e.dst_entity_id WHERE e.valid_to IS NULL"""
        if anchors:
            marks = ",".join("?" for _ in anchors)
            sql += f" AND (e.src_entity_id IN ({marks}) OR e.dst_entity_id IN ({marks}))"
            params.extend(anchors + anchors)
        sql += " ORDER BY e.weight DESC, e.edge_id LIMIT ?"
    cursor = conn.execute(sql, [*params, limit])
    keys = [col[0] for col in cursor.description]
    rows = [dict(zip(keys, row)) for row in cursor.fetchall()]
    return guard.filter_rows(rows, id_keys=("entity_id", "src_entity_id", "dst_entity_id"),
                             name_keys=("canonical_name", "source_name", "target_name"))
