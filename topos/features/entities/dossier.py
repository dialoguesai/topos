"""Entity dossiers: living per-entity summaries for significant entities.

Rules-built (no LLM required; an LLM narrative pass can layer on top later,
mirroring the brief_fallback pattern). Stored as signal_objects with
object_type='entity_dossier' under the 'relationships' dimension, and marked
disclosure='owner_only' — a dossier is the densest artifact the spine
produces and never leaves the owner tier without an explicit grant.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from ...storage.db.write_gate import commit_connection, with_db_write
from .edges import EDGE_SEMANTIC_AFFINITY, top_edges

SIGNIFICANT_MENTIONS = 3
MAX_DOSSIERS = 50


def format_connection_label(connection: Any) -> str:
    """Render a top_connections entry for prose/retrieval (string or structured)."""
    if isinstance(connection, str):
        return connection
    if not isinstance(connection, dict):
        return str(connection)
    name = (
        connection.get("canonical_name")
        or connection.get("entity_name")
        or connection.get("entity_id")
        or "?"
    )
    edge_type = connection.get("edge_type")
    weight = connection.get("weight")
    if edge_type and weight is not None:
        return f"{name} ({edge_type}, w={weight})"
    if edge_type:
        return f"{name} ({edge_type})"
    return str(name)


def _structured_connection(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entity_id": edge["entity_id"],
        "canonical_name": edge["entity_name"],
        "edge_type": edge["edge_type"],
        "weight": edge["weight"],
    }


def significant_entities(conn: sqlite3.Connection, *, limit: int = MAX_DOSSIERS) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT entity_id, entity_type, canonical_name, mention_count, first_seen, last_seen
        FROM entities
        WHERE mention_count >= ? AND is_self = 0
        ORDER BY mention_count DESC
        LIMIT ?
        """,
        (SIGNIFICANT_MENTIONS, limit),
    ).fetchall()
    return [
        {
            "entity_id": r[0],
            "entity_type": r[1],
            "canonical_name": r[2],
            "mention_count": r[3],
            "first_seen": r[4],
            "last_seen": r[5],
        }
        for r in rows
    ]


def _recent_mentions(conn: sqlite3.Connection, entity_id: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT record_id, source_id, canonical_table, surface_text, event_at
        FROM entity_mentions WHERE entity_id=?
        ORDER BY COALESCE(event_at, created_at) DESC LIMIT ?
        """,
        (entity_id, limit),
    ).fetchall()
    return [
        {
            "record_id": r[0],
            "source_id": r[1],
            "canonical_table": r[2],
            "surface_text": r[3],
            "event_at": r[4],
        }
        for r in rows
    ]


def _stat_lines_for_entity(conn: sqlite3.Connection, entity: Dict[str, Any]) -> List[str]:
    """Pull contact cadence/volume stat insights matching this entity's identifiers."""
    lines: List[str] = []
    try:
        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:messages.%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return lines
    name_tokens = set(str(entity.get("canonical_name") or "").lower().replace(".", " ").split())
    for (payload_json,) in rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        group_key = str(payload.get("group_key") or "").lower().replace(".", " ")
        if name_tokens & set(group_key.split()):
            text = str(payload.get("tag") or "").strip()
            if text:
                lines.append(text)
    return lines[:3]


def build_dossier(conn: sqlite3.Connection, entity: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = str(entity["entity_id"])
    # Observed edges and latent affinity use incompatible weight scales; keep
    # them as separate structured lists so BlackholeGuard can filter by id and
    # readers never rank a cosine against a decayed evidence count.
    observed = [
        e
        for e in top_edges(conn, entity_id, limit=12)
        if e.get("edge_type") != EDGE_SEMANTIC_AFFINITY
    ][:5]
    affinity = top_edges(
        conn, entity_id, edge_type=EDGE_SEMANTIC_AFFINITY, limit=5
    )
    mentions = _recent_mentions(conn, entity_id)
    tables = sorted(
        {str(m.get("canonical_table") or "") for m in mentions if m.get("canonical_table")}
    )
    summary_bits = [
        f"{entity['canonical_name']} — {entity['entity_type']}",
        f"{entity['mention_count']} mentions"
        + (f" across {', '.join(tables)}" if tables else ""),
    ]
    if entity.get("last_seen"):
        summary_bits.append(f"last seen {str(entity['last_seen'])[:10]}")
    stat_lines = _stat_lines_for_entity(conn, entity)

    return {
        "entity_id": entity_id,
        "entity_type": entity["entity_type"],
        "canonical_name": entity["canonical_name"],
        "summary_text": "; ".join(summary_bits) + ".",
        "top_connections": [_structured_connection(e) for e in observed],
        "affinity_connections": [_structured_connection(e) for e in affinity],
        "recent_activity": [
            f"{str(m.get('event_at') or '')[:10]} {m.get('canonical_table')}: {m.get('surface_text')}"
            for m in mentions
        ],
        "stat_lines": stat_lines,
        "mention_count": entity["mention_count"],
        "disclosure": "owner_only",
    }


def _upsert_dossier(
    conn: sqlite3.Connection,
    entity: Dict[str, Any],
    *,
    store: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build + upsert one entity dossier.

    upsert_object commits under its own (reentrant) hold — deferred to the
    batch commit when the caller is inside batched_writes."""
    from ..signal.signal_object_store import SignalObjectStore

    store = store or SignalObjectStore(conn)
    payload = build_dossier(conn, entity)
    mention_count = int(entity.get("mention_count") or 0)
    store.upsert_object(
        "relationships",
        "entity_dossier",
        f"dossier:{entity['entity_id']}",
        payload,
        source_refs=[
            {"table": "entity_mentions", "record_id": entity["entity_id"]},
        ],
        confidence=min(1.0, mention_count / 20.0),
        extractor_version="dossier_rules_v1",
    )
    return payload


def refresh_dossier_for_entity(conn: sqlite3.Connection, entity_id: str) -> bool:
    """Rewrite the dossier for one entity after a merge or split.

    A full ``refresh_dossiers`` walk is a post-rebuild job, not a request-path
    side effect: it takes a write-gate hold per significant entity and is what
    made owner Link stall the control-plane keepalive.
    """
    eid = str(entity_id or "").strip()
    if not eid:
        return False
    row = conn.execute(
        """
        SELECT entity_id, entity_type, canonical_name, mention_count, first_seen, last_seen
        FROM entities WHERE entity_id=?
        """,
        (eid,),
    ).fetchone()
    if row is None:
        return False
    entity = {
        "entity_id": row[0],
        "entity_type": row[1],
        "canonical_name": row[2],
        "mention_count": row[3],
        "first_seen": row[4],
        "last_seen": row[5],
    }
    with with_db_write():
        _upsert_dossier(conn, entity)
    commit_connection(conn)
    return True


def refresh_dossiers(conn: sqlite3.Connection) -> int:
    """Upsert dossiers for significant entities. Stable object_key = entity_id.

    Gate discipline: one short hold per entity, not one hold for the sweep —
    a full refresh walks every significant entity and a blanket hold blocks
    every other writer for the whole walk (M2.2).
    """
    from ..signal.signal_object_store import SignalObjectStore

    store = SignalObjectStore(conn)
    written = 0
    for entity in significant_entities(conn):
        with with_db_write():
            _upsert_dossier(conn, entity, store=store)
        written += 1
    # Each per-entity hold committed its own upsert; this catches any stray
    # implicit transaction so the connection never leaves here mid-write.
    commit_connection(conn)
    return written


def ensure_dossier(conn: sqlite3.Connection, entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return an active dossier for ``entity``, materializing one if missing.

    Query-time self-heal for named asks: refresh only covers significant
    entities, and supersede-without-rewrite can leave a linked person with
    mentions but no live dossier row (entity_graph fallback). Persist so the
    next load hits the store.
    """
    entity_id = str(entity.get("entity_id") or "").strip()
    if not entity_id:
        return None
    existing = load_dossier_for_entity(conn, entity_id)
    if existing is not None:
        return existing
    # Fill fields build_dossier expects when the linker only passed a stub.
    enriched = dict(entity)
    if "last_seen" not in enriched or "mention_count" not in enriched:
        row = conn.execute(
            """
            SELECT entity_type, canonical_name, mention_count, first_seen, last_seen
            FROM entities WHERE entity_id=?
            """,
            (entity_id,),
        ).fetchone()
        if row:
            enriched.setdefault("entity_type", row[0])
            enriched.setdefault("canonical_name", row[1])
            enriched.setdefault("mention_count", int(row[2] or 0))
            enriched.setdefault("first_seen", row[3])
            enriched.setdefault("last_seen", row[4])
    enriched.setdefault("canonical_name", entity_id)
    enriched.setdefault("entity_type", "person")
    enriched.setdefault("mention_count", 0)
    payload = _upsert_dossier(conn, enriched)
    commit_connection(conn)
    return payload


def load_dossier_for_entity(conn: sqlite3.Connection, entity_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT payload_json FROM signal_objects
        WHERE object_type='entity_dossier' AND object_key=? AND valid_to IS NULL
        ORDER BY updated_at DESC LIMIT 1
        """,
        (f"dossier:{entity_id}",),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None
