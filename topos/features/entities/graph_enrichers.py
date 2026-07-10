"""Materialize goals, places and conversations into the entity graph.

Extends the fact materializer's bridge (signal_objects → edges) to the other
derived stores the owner expects to SEE on their graph:

  * user_goals            -> a 'goal' node per goal, owner -pursues-> goal,
                             goal -relates_to-> entities mentioned on the
                             goal's provenance record;
  * location_events       -> owner -located_at-> place entity per place,
                             weighted by visit count (real presence, not text);
  * conversations         -> a 'conversation' node per conversation with
                             entity mentions, conversation -mentions-> entity,
                             person -participates_in-> conversation.

All edges are mz-tagged so the materializer's full-refresh cycle owns their
lifecycle, carry actor_role for the attribution overlay (goals are authored —
they come from role-gated extraction of the owner's own words; presence and
participation are participated; witnessed conversation content is observed),
and every enricher skips cleanly when its feed table is absent. Node kinds
('goal', 'conversation') ride entity_type, which the UI colors and the layer
toggles can hide.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import uuid
from typing import Dict, Optional

from .fact_materializer import _MZ_WEIGHT_FLOOR, _upsert_materialized_edge

logger = logging.getLogger("topos.features.entities.graph_enrichers")

EDGE_PURSUES = "pursues"
EDGE_RELATES_TO = "relates_to"
EDGE_MENTIONS = "mentions"
EDGE_PARTICIPATES = "participates_in"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _owner_entity(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1").fetchone()
    return str(row[0]) if row else None


def _ensure_node(conn: sqlite3.Connection, node_id: str, label: str, entity_type: str) -> None:
    from .resolver import normalize_name

    conn.execute(
        """
        INSERT OR IGNORE INTO entities
            (entity_id, entity_type, canonical_name, normalized_name, is_self,
             mention_count, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, 0, '{"mz":1}', datetime('now'), datetime('now'))
        """,
        (node_id, entity_type, label, normalize_name(label)),
    )


def _plausible_event_at(value: object) -> Optional[str]:
    """Reject epoch-zero/garbage timestamps (a 1970 edge pins the scrubber)."""
    s = str(value or "").strip()
    return s if len(s) >= 4 and s[:4].isdigit() and int(s[:4]) >= 2000 else None


def _record_event_at(conn: sqlite3.Connection, record_id: str) -> Optional[str]:
    """EVENT time of a canonical record (timeline registry, mentions fallback)."""
    try:
        row = conn.execute(
            "SELECT event_at FROM timeline WHERE record_id=? AND event_at IS NOT NULL LIMIT 1",
            (record_id,),
        ).fetchone()
        if row:
            plausible = _plausible_event_at(row[0])
            if plausible:
                return plausible
    except sqlite3.OperationalError:
        pass
    row = conn.execute(
        "SELECT event_at FROM entity_mentions WHERE record_id=? AND event_at IS NOT NULL LIMIT 1",
        (record_id,),
    ).fetchone()
    return _plausible_event_at(row[0]) if row else None


def _materialize_goals(conn: sqlite3.Connection, owner: Optional[str]) -> int:
    if not _table_exists(conn, "user_goals"):
        return 0
    from .resolver import normalize_name

    # Group by normalized goal TEXT: re-extraction mints a fresh goal_id for
    # the same goal every run, which minted duplicate nodes. One goal = one
    # node; its edge window spans earliest→latest occurrence.
    grouped: Dict[str, Dict] = {}
    for goal_id, record_id, goal_text, created_at in conn.execute(
        "SELECT goal_id, record_id, goal_text, created_at FROM user_goals"
    ).fetchall():
        text = str(goal_text or "").strip()
        if not text:
            continue
        key = normalize_name(text)
        g = grouped.setdefault(key, {"text": text, "goal_id": str(goal_id), "events": [], "records": []})
        # Date by WHEN IT HAPPENED (source record's event time), not when
        # extraction ran — created_at is only the last-resort fallback.
        event_at = (_record_event_at(conn, str(record_id)) if record_id else None) or created_at
        if event_at:
            g["events"].append(str(event_at))
        if record_id:
            g["records"].append((str(record_id), str(event_at) if event_at else None))

    edges = 0
    for key, g in grouped.items():
        node_id = f"goal_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
        _ensure_node(conn, node_id, g["text"], "goal")
        first_at = min(g["events"]) if g["events"] else None
        last_at = max(g["events"]) if g["events"] else None
        occurrences = max(len(g["records"]), 1)
        if owner:
            _upsert_materialized_edge(
                conn, src=owner, dst=node_id, edge_type=EDGE_PURSUES,
                weight=min(_MZ_WEIGHT_FLOOR + 0.25 * (occurrences - 1), 6.0),
                valid_from=first_at, valid_to=None, last_event_at=last_at,
                statement=f"pursues: {g['text'][:80]}" + (f" (×{occurrences})" if occurrences > 1 else ""),
                source_object_id=g["goal_id"], actor_role="authored",
            )
            edges += 1
        # Entities mentioned on the goal's provenance records relate to the goal.
        seen_entities: Dict[str, str] = {}
        for record_id, event_at in g["records"]:
            for (ent_id,) in conn.execute(
                "SELECT DISTINCT entity_id FROM entity_mentions WHERE record_id=?",
                (record_id,),
            ):
                if str(ent_id) == owner or str(ent_id) == node_id:
                    continue
                prev = seen_entities.get(str(ent_id))
                if event_at and (prev is None or event_at > prev):
                    seen_entities[str(ent_id)] = event_at
                elif prev is None:
                    seen_entities.setdefault(str(ent_id), "")
        for ent_id, ev in seen_entities.items():
            _upsert_materialized_edge(
                conn, src=node_id, dst=ent_id, edge_type=EDGE_RELATES_TO,
                weight=_MZ_WEIGHT_FLOOR, valid_from=ev or first_at, valid_to=None,
                statement="goal relates to (from its source record)",
                source_object_id=g["goal_id"], actor_role="authored",
            )
            edges += 1
    return edges


def _materialize_places(conn: sqlite3.Connection, owner: Optional[str]) -> int:
    if not owner or not _table_exists(conn, "location_events"):
        return 0
    from .resolver import EntityResolver, is_valid_entity_surface

    resolver = EntityResolver(conn)
    edges = 0
    rows = conn.execute(
        """
        SELECT place_name, COUNT(*), MIN(event_at), MAX(event_at)
        FROM location_events
        WHERE place_name IS NOT NULL AND place_name != ''
        GROUP BY place_name
        """
    ).fetchall()
    for place_name, visits, first_at, last_at in rows:
        name = str(place_name).strip()
        if not is_valid_entity_surface(name):
            continue
        try:
            place_id, _tier = resolver.resolve(name, entity_type="place")
        except ValueError:
            continue
        if place_id == owner:
            continue
        # Repeat presence outweighs a single text mention: scale with visits.
        weight = min(_MZ_WEIGHT_FLOOR + float(visits) * 0.25, 10.0)
        _upsert_materialized_edge(
            conn, src=owner, dst=place_id, edge_type="located_at",
            weight=weight, valid_from=first_at, valid_to=None,
            statement=f"visited {name} ×{visits}", source_object_id=f"loc:{name[:40]}",
            actor_role="participated",
        )
        conn.execute(
            """
            UPDATE entity_edges
            SET last_event_at=?, metadata_json=json_patch(COALESCE(metadata_json,'{}'), ?)
            WHERE src_entity_id=? AND dst_entity_id=? AND edge_type='located_at' AND valid_to IS NULL
            """,
            (last_at, f'{{"visit_count": {int(visits)}}}', owner, place_id),
        )
        edges += 1
    return edges


def _materialize_conversations(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "conversations") or not _table_exists(conn, "conversation_messages"):
        return 0
    edges = 0
    # Conversations that actually mention entities get a node; empty ones don't
    # clutter the graph.
    rows = conn.execute(
        """
        SELECT cm.conversation_id, m.entity_id, COUNT(*), MAX(m.event_at)
        FROM entity_mentions m
        JOIN conversation_messages cm ON cm.message_id = m.record_id
        GROUP BY cm.conversation_id, m.entity_id
        """
    ).fetchall()
    conv_ids = {str(r[0]) for r in rows}
    for conv_id in conv_ids:
        label = conv_id if len(conv_id) <= 40 else conv_id[:37] + "…"
        _ensure_node(conn, f"conv_{conv_id}", label, "conversation")
    for conv_id, entity_id, count, last_at in rows:
        _upsert_materialized_edge(
            conn, src=f"conv_{conv_id}", dst=str(entity_id), edge_type=EDGE_MENTIONS,
            weight=min(_MZ_WEIGHT_FLOOR + float(count) * 0.25, 8.0),
            valid_from=last_at, valid_to=None,
            statement=f"mentioned in conversation ×{count}",
            source_object_id=f"conv:{conv_id}", actor_role="observed",
        )
        edges += 1
    if _table_exists(conn, "conversation_participants"):
        for conv_id, contact_id in conn.execute(
            "SELECT conversation_id, contact_id FROM conversation_participants "
            "WHERE contact_id IS NOT NULL"
        ).fetchall():
            if str(conv_id) not in conv_ids:
                continue
            ent = conn.execute(
                "SELECT entity_id FROM entities WHERE contact_id=? LIMIT 1", (str(contact_id),)
            ).fetchone()
            if not ent:
                continue
            _upsert_materialized_edge(
                conn, src=str(ent[0]), dst=f"conv_{conv_id}", edge_type=EDGE_PARTICIPATES,
                weight=_MZ_WEIGHT_FLOOR, valid_from=None, valid_to=None,
                statement="participated in conversation",
                source_object_id=f"conv:{conv_id}", actor_role="participated",
            )
            edges += 1
    return edges


def materialize_graph_enrichments(conn: sqlite3.Connection) -> Dict[str, int]:
    """Materialize goals + places + conversations. Idempotent (mz upserts)."""
    owner = _owner_entity(conn)
    out = {
        "goal_edges": _materialize_goals(conn, owner),
        "place_edges": _materialize_places(conn, owner),
        "conversation_edges": _materialize_conversations(conn),
    }
    conn.commit()
    return out
