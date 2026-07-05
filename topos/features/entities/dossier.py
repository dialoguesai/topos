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

from .edges import top_edges

SIGNIFICANT_MENTIONS = 3
MAX_DOSSIERS = 50


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
    edges = top_edges(conn, entity_id, limit=8)
    mentions = _recent_mentions(conn, entity_id)
    tables = sorted(
        {str(m.get("canonical_table") or "") for m in mentions if m.get("canonical_table")}
    )
    connections = [
        f"{e['entity_name']} ({e['edge_type']}, w={e['weight']})" for e in edges[:5]
    ]
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
        "top_connections": connections,
        "recent_activity": [
            f"{str(m.get('event_at') or '')[:10]} {m.get('canonical_table')}: {m.get('surface_text')}"
            for m in mentions
        ],
        "stat_lines": stat_lines,
        "mention_count": entity["mention_count"],
        "disclosure": "owner_only",
    }


def refresh_dossiers(conn: sqlite3.Connection) -> int:
    """Upsert dossiers for significant entities. Stable object_key = entity_id."""
    from ..signal.signal_object_store import SignalObjectStore

    store = SignalObjectStore(conn)
    written = 0
    for entity in significant_entities(conn):
        payload = build_dossier(conn, entity)
        source_refs = [
            {"table": "entity_mentions", "record_id": entity["entity_id"]},
        ]
        store.upsert_object(
            "relationships",
            "entity_dossier",
            f"dossier:{entity['entity_id']}",
            payload,
            source_refs=source_refs,
            confidence=min(1.0, entity["mention_count"] / 20.0),
            extractor_version="dossier_rules_v1",
        )
        written += 1
    conn.commit()
    return written


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
