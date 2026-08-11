"""Materialize signal_objects (facts + topic clusters) into the entity graph.

The entity graph the UI reads (entities + entity_edges) is a co-occurrence graph
only. The owner's extracted *statements* — SPO facts and topic clusters — live in
signal_objects, carry entity links and bi-temporal validity, and never reach the
graph. This materializer bridges them into the SAME spine tables so the existing
/entities/graph endpoint and its UI (labeled edges, temporal scrubber, dashing)
render them with no frontend change:

  * topic clusters (object_type='top_topics') -> a topic hub node linked by
    ``discusses`` edges to each related entity;
  * SPO facts (object_type='fact') -> a subject->object edge whose type is the
    predicate (place predicates map to ``located_at``), carrying the fact's
    validity window and confidence.

Materialized edges are tagged (metadata_json.mz = 1) and given a weight above the
co-occurrence noise floor so they show by default; a full refresh drops the prior
tagged edges first, so re-running is idempotent. String-valued fact objects are
resolved to entity nodes (the resolve-to-nodes choice) so the graph stays maximally
connected. Bounded by the current signal_objects; grows as extraction produces
more facts.

``discusses`` edges are dated from topic-cluster *member* activity (timeline /
embeddings), not ``signal_objects.valid_from``, so Active-in matches goal dating.
Existing nodes pick up the new stamps on the next ``rebuild_entity_graph`` /
``materialize_signal_objects_to_graph`` (debounced via ``graph_refresh`` after
enrichment, or call rebuild manually).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Dict, Optional, Tuple

logger = logging.getLogger("topos.features.entities.fact_materializer")

# Fact predicates that denote a place; their object resolves as a place entity
# and the edge reads as located_at.
_PLACE_PREDICATES = {
    "lived_in", "lives_in", "located_at", "located_in", "based_in",
    "from", "born_in", "traveled_to", "visited",
}

# Weight floor for materialized edges: above the default min_weight (1.5) so a
# statement always shows, unlike single co-occurrences.
_MZ_WEIGHT_FLOOR = 2.0


def _now_iso() -> str:
    from .edges import _now_iso as edges_now

    return edges_now()


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
    try:
        row = conn.execute(
            "SELECT event_at FROM entity_mentions WHERE record_id=? AND event_at IS NOT NULL LIMIT 1",
            (record_id,),
        ).fetchone()
        return _plausible_event_at(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


def _member_event_at(conn: sqlite3.Connection, record_id: str) -> Optional[str]:
    """Member activity time: timeline/mentions, then embedding event_at."""
    ts = _record_event_at(conn, record_id)
    if ts:
        return ts
    try:
        row = conn.execute(
            "SELECT MAX(event_at) FROM signal_embeddings "
            "WHERE record_id=? AND event_at IS NOT NULL",
            (record_id,),
        ).fetchone()
        return _plausible_event_at(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


def _cluster_activity_window(
    conn: sqlite3.Connection, cluster_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """Earliest/latest member activity for a topic cluster (mirrors goal dating).

    Resolution per member record_id: timeline → entity_mentions →
    signal_embeddings.event_at → topic_cluster_members.created_at.
    """
    try:
        rows = conn.execute(
            "SELECT record_id, created_at FROM topic_cluster_members WHERE cluster_id=?",
            (cluster_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    events: list[str] = []
    for record_id, created_at in rows:
        ts = _member_event_at(conn, str(record_id)) if record_id else None
        if not ts:
            ts = _plausible_event_at(created_at)
        if ts:
            events.append(ts)
    if not events:
        return None, None
    return min(events), max(events)


# Role resolution reads one canonical row per member; clusters run to hundreds
# of members, so sample rather than walk them all. The dominant role of a few
# hundred members is not going to be overturned by the tail.
_ROLE_SAMPLE_PER_CLUSTER = 200


def _cluster_actor_role(conn: sqlite3.Connection, cluster_id: str) -> str:
    """Attribution role for a topic-cluster edge, from its members' families.

    A topic cluster asserts nothing — nobody "said" a cluster — so these edges
    carried no ``asserted_by`` and fell through ``_asserted_by_role(None)``,
    whose "owner" default stamped every one of them ``authored``. On a live node
    that put GitHub-feed exposure in the owner's own voice (19 recent edges) and
    skewed the attribution overlay to 91.5% authored.

    The role is computed per member from the canonical family it belongs to and
    the MAJORITY wins, with ties breaking toward the LESS-attributing role —
    the direction roles.py requires for ambiguity. Members we cannot place at
    all yield ``ambient``: "we do not know whose words these are" is a true
    statement, "the owner wrote it" is not.
    """
    from ..provenance.roles import ROLE_AMBIENT
    from .maintenance import _dominant_role, _record_role_map

    try:
        rows = conn.execute(
            """
            SELECT DISTINCT t.record_id, t.canonical_table, t.source_id
            FROM topic_cluster_members m
            JOIN timeline t ON t.record_id = m.record_id
            WHERE m.cluster_id = ?
            LIMIT ?
            """,
            (str(cluster_id), _ROLE_SAMPLE_PER_CLUSTER),
        ).fetchall()
    except sqlite3.OperationalError:
        return ROLE_AMBIENT
    by_record = {
        str(rid): {"table": str(table or ""), "source_id": str(source_id or "")}
        for rid, table, source_id in rows
        if rid
    }
    if not by_record:
        return ROLE_AMBIENT
    # Same role map the co-occurrence edges use, so a topic edge and an organic
    # edge over the same records never disagree about whose words they are. It
    # reads the actual canonical row, so is_from_self and the source's posture
    # both count — passing a bare table name would collapse the owner's own
    # messages to "observed", trading one wrong attribution for another.
    mix: Dict[str, int] = {}
    for role in _record_role_map(conn, by_record).values():
        mix[role] = mix.get(role, 0) + 1
    return _dominant_role(mix) if mix else ROLE_AMBIENT


def _asserted_by_role(asserted_by: Optional[str]) -> str:
    """Map a fact's asserted_by onto the record-role spectrum.

    owner-stated facts are expression (authored); a contact's claim about the
    owner is participated (they were in the exchange, the owner didn't say it);
    assistant-derived facts are observed (inferred, not stated); anything else
    (page-author, unknown) is ambient exposure.
    """
    v = str(asserted_by or "owner").strip().lower()
    if v in ("owner", "self", "me"):
        return "authored"
    if v.startswith("contact"):
        return "participated"
    if v in ("assistant", "ai"):
        return "observed"
    return "ambient"


def _entity_exists(conn: sqlite3.Connection, entity_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone() is not None


def _ensure_topic_node(conn: sqlite3.Connection, topic_id: str, label: str) -> None:
    from .resolver import normalize_name

    conn.execute(
        """
        INSERT OR IGNORE INTO entities
            (entity_id, entity_type, canonical_name, normalized_name, is_self,
             mention_count, metadata_json, created_at, updated_at)
        VALUES (?, 'topic', ?, ?, 0, 0, '{"mz":1}', ?, ?)
        """,
        (topic_id, label, normalize_name(label), _now_iso(), _now_iso()),
    )


def _upsert_materialized_edge(
    conn: sqlite3.Connection,
    *,
    src: str,
    dst: str,
    edge_type: str,
    weight: float,
    valid_from: Optional[str],
    valid_to: Optional[str],
    statement: str,
    source_object_id: str,
    asserted_by: Optional[str] = None,
    actor_role: Optional[str] = None,
    last_event_at: Optional[str] = None,
) -> None:
    """Idempotent directed edge with a materialized marker + carried validity.

    ``actor_role`` overrides the asserted_by-derived tier — used by enrichers
    whose provenance isn't an assertion (presence, participation, witnessing).
    ``last_event_at`` defaults to valid_from — pass explicitly when the edge's
    evidence spans a window (e.g. a goal recurring across months).
    """
    if not src or not dst or src == dst:
        return
    meta = json.dumps(
        {
            "mz": 1,
            "statement": statement,
            "source_object_id": source_object_id,
            **({"asserted_by": asserted_by} if asserted_by else {}),
            # Provenance tier for the attribution overlay: who asserted the
            # fact maps onto the personal→ambient spectrum.
            "actor_role": actor_role or _asserted_by_role(asserted_by),
        }
    )
    row = conn.execute(
        """
        SELECT edge_id FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
        """,
        (src, dst, edge_type),
    ).fetchone()
    effective_last = last_event_at or valid_from
    if row is None:
        conn.execute(
            """
            INSERT INTO entity_edges
                (edge_id, src_entity_id, dst_entity_id, edge_type, weight,
                 evidence_count, last_event_at, valid_from, valid_to, metadata_json)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                f"fmz_{uuid.uuid4().hex[:16]}",
                src, dst, edge_type, float(weight),
                effective_last, valid_from or _now_iso(), valid_to, meta,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE entity_edges
            SET weight=?, valid_from=?, valid_to=?, last_event_at=?, metadata_json=?,
                updated_at=datetime('now')
            WHERE edge_id=?
            """,
            (float(weight), valid_from, valid_to, effective_last, meta, row[0]),
        )


def materialize_signal_objects_to_graph(
    conn: sqlite3.Connection,
    *,
    resolve_strings: bool = True,
) -> Dict[str, int]:
    """Refresh materialized fact/topic edges from active signal_objects.

    Returns counts of topic and fact edges written.
    """
    from .edges import EDGE_DISCUSSES, EDGE_LOCATED_AT
    from .resolver import EntityResolver, is_valid_entity_surface, normalize_name
    from .resolver import value_label_surfaces

    resolver = EntityResolver(conn)
    resolver.seed_from_contacts()

    # Surfaces the model labelled DATE/TIME/CARDINAL/… Both lanes below resolve
    # bare strings, so map_ner_type — the extraction lane's guard — cannot run
    # here; without this, dates and quantities become graph nodes.
    values = value_label_surfaces(conn)

    def _mintable(surface: str) -> bool:
        return is_valid_entity_surface(surface) and normalize_name(surface) not in values

    # Full refresh: drop previously-materialized edges (keep organic evidence edges).
    conn.execute("DELETE FROM entity_edges WHERE json_extract(metadata_json, '$.mz') = 1")

    topic_edges = 0
    fact_edges = 0

    # 1) Topic clusters -> hub node + discusses edges to related entities.
    rows = conn.execute(
        """
        SELECT object_id, object_key, payload_json, valid_from, valid_to
        FROM signal_objects
        WHERE object_type = 'top_topics' AND valid_to IS NULL
        """
    ).fetchall()
    for object_id, object_key, payload_json, valid_from, valid_to in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        tag = str(payload.get("tag") or "").strip()
        related = payload.get("related_entities") or []
        if not tag or not isinstance(related, list) or not related:
            continue
        topic_id = f"topic_{object_key}"
        _ensure_topic_node(conn, topic_id, tag)
        # Date by WHEN members were active, not when the signal_object row was
        # first inserted — otherwise Active-in drops live topics (or keeps
        # stale ones) while goals (event-dated) flip the other way.
        first_at, last_at = _cluster_activity_window(conn, str(object_key))
        edge_from = first_at or valid_from
        edge_last = last_at or first_at
        cluster_role = _cluster_actor_role(conn, str(object_key))
        linked = 0
        for name in related:
            if not _mintable(str(name)):
                continue
            try:
                ent_id, _tier = resolver.resolve(str(name))
            except ValueError:
                continue
            if ent_id == topic_id:
                continue
            _upsert_materialized_edge(
                conn, src=topic_id, dst=ent_id, edge_type=EDGE_DISCUSSES,
                weight=_MZ_WEIGHT_FLOOR, valid_from=edge_from, valid_to=None,
                last_event_at=edge_last,
                statement=f"topic: {tag}", source_object_id=object_id,
                actor_role=cluster_role,
            )
            topic_edges += 1
            linked += 1
        if linked == 0:
            # No real entities to link — drop the orphan hub we just made.
            conn.execute(
                "DELETE FROM entities WHERE entity_id=? AND mention_count=0", (topic_id,)
            )

    # 2) SPO facts -> subject->object labeled edges.
    frows = conn.execute(
        """
        SELECT object_id, payload_json, valid_from, valid_to, confidence
        FROM signal_objects
        WHERE object_type = 'fact' AND valid_to IS NULL
        """
    ).fetchall()
    for object_id, payload_json, valid_from, valid_to, confidence in frows:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError):
            continue
        subj = str(payload.get("subject_entity_id") or "").strip()
        pred = str(payload.get("predicate") or "").strip()
        obj_val = str(payload.get("object_value") or "").strip()
        obj_id = str(payload.get("object_entity_id") or "").strip()
        if not subj or not pred or not obj_val or not _entity_exists(conn, subj):
            continue

        is_place = pred in _PLACE_PREDICATES
        if not obj_id:
            if not resolve_strings or not _mintable(obj_val):
                continue
            try:
                obj_id, _tier = resolver.resolve(
                    obj_val, entity_type="place" if is_place else None
                )
            except ValueError:
                continue
        elif not _entity_exists(conn, obj_id):
            continue

        edge_type = EDGE_LOCATED_AT if is_place else pred
        weight = max(_MZ_WEIGHT_FLOOR, float(confidence or 0.7) * 3.0)
        _upsert_materialized_edge(
            conn, src=subj, dst=obj_id, edge_type=edge_type, weight=weight,
            valid_from=valid_from, valid_to=valid_to,
            statement=f"{pred.replace('_', ' ')} {obj_val}", source_object_id=object_id,
            asserted_by=payload.get("asserted_by"),
        )
        fact_edges += 1

    # 3) Purge value surfaces minted by EARLIER runs of this lane, which ran
    # before the guard existed ("an hour", "this week", "Mon-Wed" — 51 such
    # nodes on the first live node checked). Bounded hard on purpose:
    # mention_count = 0 means the surface was never extracted as an entity in
    # its own right, and the no-edge check runs after the refresh above, so a
    # real entity that merely shares a value-ish surface is never touched.
    purged = 0
    for entity_id, _norm in [
        (eid, norm)
        for eid, norm in conn.execute(
            "SELECT entity_id, normalized_name FROM entities WHERE COALESCE(mention_count, 0) = 0"
        ).fetchall()
        if str(norm or "") in values
    ]:
        if conn.execute(
            "SELECT 1 FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=? LIMIT 1",
            (entity_id, entity_id),
        ).fetchone():
            continue
        conn.execute("DELETE FROM entities WHERE entity_id=?", (entity_id,))
        purged += 1
    if purged:
        logger.info("materializer purged %d value-surface entities (dates/quantities)", purged)

    conn.commit()
    return {"topic_edges": topic_edges, "fact_edges": fact_edges, "purged_value_nodes": purged}
