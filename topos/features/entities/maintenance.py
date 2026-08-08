"""Entity-graph maintenance: rebuild evidence edges from surviving mentions.

Entity edges (co_occurrence, communicates_with) are written incrementally at
ingest time by EntitiesJob. There is no ingest-independent way to recompute
them, so:

  * after a source deletion the decayed weights can't subtract removed evidence
    (lifecycle/derived_scrub calls in here), and
  * if extraction was disabled/partial for a stretch, the graph under-counts.

`rebuild_evidence_edges` recomputes both evidence edge types deterministically:

  * ``co_occurrence`` from `entity_mentions` (entities named in the same record);
  * ``communicates_with`` from **thread co-participation** (distinct senders /
    conversation_participants who share a conversation) — NOT from
    sender→NER-mention links. Mention-only names (gossip / third parties) must
    never mint a talked-to edge (P3.2 / IMB7).

Cheap at personal scale; no NER re-run. `part_of` edges are structural and are
left untouched; closed/superseded history rows are preserved.

Note the historical bug this fixes: the previous rebuild deleted
`communicates_with` edges but only recreated `co_occurrence`, so every
source-scrub silently and permanently dropped every co-participation edge.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger("topos.features.entities.maintenance")

# Canonical message tables that carry a sender we can attribute a
# communicates_with edge to. Maps table -> sender column name.
_SENDER_TABLES = {
    "conversation_messages": "sender_id",
    "ai_chat_messages": "sender_id",
    "conversation_message": "sender_id",
}

_SELF_SENDER_TOKENS = frozenset({"self", "me", "owner", "user"})

# conversation_id column candidates per message table.
_CONVERSATION_ID_COLUMNS = (
    "conversation_id",
    "chat_id",
    "thread_id",
)


def _pk_column(conn: sqlite3.Connection, table: str) -> Optional[str]:
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    pk = [c for c in cols if c[5]]  # cid, name, type, notnull, dflt, pk
    if pk:
        return str(pk[0][1])
    names = {str(c[1]) for c in cols}
    for candidate in ("message_id", "record_id", "id"):
        if candidate in names:
            return candidate
    return None


def _load_senders(conn: sqlite3.Connection, tables: set[str]) -> Dict[str, str]:
    """record_id -> raw sender identifier, across the message tables in `tables`."""
    senders: Dict[str, str] = {}
    for table in tables:
        sender_col = _SENDER_TABLES.get(table)
        if not sender_col:
            continue
        id_col = _pk_column(conn, table)
        if not id_col:
            continue
        try:
            rows = conn.execute(
                f"SELECT {id_col}, {sender_col} FROM {table} "
                f"WHERE {sender_col} IS NOT NULL AND {sender_col} != ''"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug("sender load failed for %s: %s", table, exc)
            continue
        for rec_id, sender in rows:
            senders[str(rec_id)] = str(sender)
    return senders


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    try:
        return {str(c[1]) for c in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def lookup_person_entity(
    conn: sqlite3.Connection,
    sender_raw: str,
    *,
    is_from_self: bool = False,
) -> Optional[str]:
    """Resolve a message sender to an existing person entity — never mint.

    Talked-to edges must only bind known participants (contacts / is_self /
    identifier-linked people). Bare NER surfaces and gossip names stay out.
    """
    if is_from_self or str(sender_raw or "").strip().lower() in _SELF_SENDER_TOKENS:
        row = conn.execute(
            "SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])

    raw = str(sender_raw or "").strip()
    if not raw:
        return None
    needle = raw.lower()

    # contact_id exact (conversation_participants path)
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE contact_id=? AND entity_type='person' LIMIT 1",
        (raw,),
    ).fetchone()
    if row:
        return str(row[0])

    # identifiers_json contains the messenger sender_id / handle
    try:
        for entity_id, identifiers_json in conn.execute(
            "SELECT entity_id, identifiers_json FROM entities "
            "WHERE entity_type='person' AND identifiers_json IS NOT NULL "
            "AND identifiers_json != '[]'"
        ):
            try:
                identifiers = json.loads(identifiers_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if needle in {str(i).strip().lower() for i in identifiers if i}:
                return str(entity_id)
    except sqlite3.Error:
        pass

    # contact_identifiers join
    try:
        row = conn.execute(
            """
            SELECT e.entity_id FROM entities e
            JOIN contact_identifiers ci ON ci.contact_id = e.contact_id
            WHERE e.entity_type='person' AND lower(ci.identifier)=?
            LIMIT 1
            """,
            (needle,),
        ).fetchone()
        if row:
            return str(row[0])
    except sqlite3.Error:
        pass

    # Exact display / normalized name (contact-seeded people only — avoids
    # binding mention-only entities that share a first name).
    row = conn.execute(
        """
        SELECT entity_id FROM entities
        WHERE entity_type='person' AND contact_id IS NOT NULL
          AND (lower(canonical_name)=? OR normalized_name=?)
        LIMIT 1
        """,
        (needle, needle),
    ).fetchone()
    if row:
        return str(row[0])

    return None


def _load_conversation_participation(
    conn: sqlite3.Connection,
    *,
    conversation_ids: Optional[Iterable[str]] = None,
) -> Dict[str, List[Dict[str, object]]]:
    """conversation_id -> list of {entity_id, event_at, role} participant events.

    Sources: message senders (authored/observed/…) plus conversation_participants
    membership rows. Mention-only entities never appear here.
    """
    conv_filter = {str(c) for c in conversation_ids} if conversation_ids is not None else None
    by_conv: Dict[str, List[Dict[str, object]]] = {}

    for table, sender_col in _SENDER_TABLES.items():
        cols = _table_columns(conn, table)
        if not cols or sender_col not in cols:
            continue
        id_col = _pk_column(conn, table)
        if not id_col:
            continue
        conv_col = next((c for c in _CONVERSATION_ID_COLUMNS if c in cols), None)
        if not conv_col:
            continue
        select_cols = [id_col, conv_col, sender_col]
        has_self = "is_from_self" in cols
        has_role = "actor_role" in cols
        has_event = "event_at" in cols
        if has_self:
            select_cols.append("is_from_self")
        if has_role:
            select_cols.append("actor_role")
        if has_event:
            select_cols.append("event_at")
        try:
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM {table} "
                f"WHERE {sender_col} IS NOT NULL AND {sender_col} != '' "
                f"AND {conv_col} IS NOT NULL AND {conv_col} != ''"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug("participation load failed for %s: %s", table, exc)
            continue
        for row in rows:
            conv_id = str(row[1])
            if conv_filter is not None and conv_id not in conv_filter:
                continue
            sender_raw = str(row[2])
            idx = 3
            is_from_self = False
            if has_self:
                is_from_self = row[idx] in (1, True, "1")
                idx += 1
            role = "ambient"
            if has_role:
                role = str(row[idx] or "").strip() or role
                idx += 1
            event_at = None
            if has_event:
                event_at = row[idx]
            entity_id = lookup_person_entity(
                conn, sender_raw, is_from_self=is_from_self
            )
            if not entity_id:
                continue
            if not has_role:
                # Cheap posture fallback when actor_role is absent.
                role = "authored" if is_from_self else "participated"
            by_conv.setdefault(conv_id, []).append(
                {"entity_id": entity_id, "event_at": event_at, "role": role}
            )

    # Membership rows cover silent participants (read receipts / added-but-quiet).
    try:
        part_rows = conn.execute(
            """
            SELECT conversation_id, contact_id FROM conversation_participants
            WHERE contact_id IS NOT NULL AND contact_id != ''
            """
        ).fetchall()
    except sqlite3.Error:
        part_rows = []
    for conv_id, contact_id in part_rows:
        conv_key = str(conv_id)
        if conv_filter is not None and conv_key not in conv_filter:
            continue
        entity_id = lookup_person_entity(conn, str(contact_id))
        if not entity_id:
            continue
        events = by_conv.setdefault(conv_key, [])
        if any(e["entity_id"] == entity_id for e in events):
            continue
        events.append(
            {"entity_id": entity_id, "event_at": None, "role": "participated"}
        )

    return by_conv


def _participation_edge_events(
    by_conv: Dict[str, List[Dict[str, object]]],
) -> Iterator[Tuple[str, str, Optional[str], str]]:
    """Yield (src, dst, event_at, role) communicates_with evidence tuples.

    One tuple per message event × co-participant — the shared pair expansion
    for both the incremental fold (update_edge per tuple) and the rebuild's
    in-memory accumulator, so the two paths cannot drift.
    """
    for _conv_id, events in by_conv.items():
        # Distinct participants in this thread.
        participants = list(dict.fromkeys(str(e["entity_id"]) for e in events))
        if len(participants) < 2:
            continue
        # Evidence: each message event links its sender to every other
        # co-participant (talked-to), never to mention-only third parties.
        for ev in events:
            src = str(ev["entity_id"])
            role = str(ev.get("role") or "participated")
            event_at = ev.get("event_at")
            for dst in participants:
                if dst == src:
                    continue
                yield src, dst, (str(event_at) if event_at else None), role


class _EdgeAccumulator:
    """In-memory replay of ``update_edge``'s fold for a from-scratch rebuild.

    The rebuild deletes every active evidence edge before rewriting, so each
    replayed observation lands on an edge whose state this dict fully owns —
    the whole fold can run in Python OUTSIDE the write gate, shrinking the
    write phase to one DELETE plus one batched INSERT. Delegates the
    decay-then-add rule to :func:`edges.fold_edge_observation`, the same code
    ingest uses, so a rebuild converges on identical weights.
    """

    def __init__(self) -> None:
        from .edges import _canonical_order, fold_edge_observation

        self._canon = _canonical_order
        self._fold = fold_edge_observation
        self.edges: Dict[tuple, Dict[str, object]] = {}

    def add(self, src: str, dst: str, edge_type: str, event_at: Optional[str]) -> None:
        if not src or not dst or src == dst:
            return  # same guards as update_edge
        src, dst = self._canon(src, dst, edge_type)
        state = self.edges.get((src, dst, edge_type))
        if state is None:
            # update_edge's fresh-row branch: weight=increment, count=1.
            self.edges[(src, dst, edge_type)] = {
                "weight": 1.0,
                "count": 1,
                "last": event_at,
            }
            return
        weight, count, last = self._fold(
            state["weight"], state["count"], state["last"], event_at
        )
        state["weight"] = weight
        state["count"] = count
        state["last"] = last


def fold_communicates_with_edges(
    conn: sqlite3.Connection,
    *,
    conversation_ids: Optional[Iterable[str]] = None,
    clear_active: bool = False,
    participation: Optional[Dict[str, List[Dict[str, object]]]] = None,
) -> Tuple[int, Dict[tuple, Dict[str, int]]]:
    """Write communicates_with edges from thread co-participation.

    When ``clear_active`` is True (rebuild path), deletes active
    communicates_with rows first. Returns (update_count, edge_role_mix).

    ``participation`` accepts a precomputed
    :func:`_load_conversation_participation` result so the full rebuild can run
    that load — a per-message entity resolution scan, the expensive part —
    outside the write gate. When omitted it is loaded here (incremental path).
    """
    from .edges import EDGE_COMMUNICATES, _canonical_order, update_edge

    if clear_active:
        conn.execute(
            "DELETE FROM entity_edges "
            "WHERE edge_type='communicates_with' AND valid_to IS NULL"
        )

    by_conv = (
        participation
        if participation is not None
        else _load_conversation_participation(conn, conversation_ids=conversation_ids)
    )
    edge_roles: Dict[tuple, Dict[str, int]] = {}
    comm = 0

    def _tally(src: str, dst: str, role: str) -> None:
        key = _canonical_order(src, dst, EDGE_COMMUNICATES) + (EDGE_COMMUNICATES,)
        mix = edge_roles.setdefault(key, {})
        mix[role] = mix.get(role, 0) + 1

    for src, dst, event_at, role in _participation_edge_events(by_conv):
        update_edge(
            conn,
            src_entity_id=src,
            dst_entity_id=dst,
            edge_type=EDGE_COMMUNICATES,
            event_at=event_at,
        )
        _tally(src, dst, role)
        comm += 1

    return comm, edge_roles


def _record_role_map(conn: sqlite3.Connection, by_record: Dict[str, Dict]) -> Dict[str, str]:
    """record_id -> provenance role (authored>addressed>participated>observed>ambient).

    Uses the record's materialized actor_role column when present, else computes
    record_role() from the canonical row + effective source posture — the same
    plan-§3.1 rules the extraction gates use. Unresolvable records fall back to
    the role computed from the table name alone.
    """
    from ..provenance.roles import record_role

    try:
        from ...sources.registry import effective_posture
    except Exception:  # pragma: no cover - registry optional in stripped builds
        effective_posture = None  # type: ignore[assignment]

    posture_cache: Dict[str, Optional[str]] = {}

    def _posture(source_id: str) -> Optional[str]:
        if source_id not in posture_cache:
            posture = None
            if effective_posture is not None and source_id:
                try:
                    posture = effective_posture(source_id, "", conn)
                except Exception:
                    posture = None
            posture_cache[source_id] = posture
        return posture_cache[source_id]

    roles: Dict[str, str] = {}
    for record_id, rec in by_record.items():
        table = str(rec.get("table") or "")
        source_id = str(rec.get("source_id") or "")
        row: Dict[str, object] = {}
        if table and table.replace("_", "").isalnum():
            try:
                cols = [str(c[1]) for c in conn.execute(f"PRAGMA table_info({table})")]
                id_col = next(
                    (c for c in ("record_id", "message_id", "entry_id", "event_id", "id") if c in cols),
                    None,
                )
                if id_col:
                    raw = conn.execute(
                        f"SELECT * FROM {table} WHERE {id_col}=? LIMIT 1", (record_id,)
                    ).fetchone()
                    if raw is not None:
                        row = dict(zip(cols, raw))
            except sqlite3.Error:
                row = {}
        materialized = str(row.get("actor_role") or "").strip()
        if materialized:
            roles[record_id] = materialized
            continue
        try:
            roles[record_id] = record_role(row, table=table, posture=_posture(source_id))
        except Exception:
            roles[record_id] = "ambient"
    return roles


def _dominant_role(mix: Dict[str, int]) -> str:
    """Majority role; ties resolve to the more attributing role (ROLES order)."""
    from ..provenance.roles import ROLES

    best, best_count = "ambient", -1
    for role in ROLES:  # precedence order: authored first
        count = mix.get(role, 0)
        if count > best_count:
            best, best_count = role, count
    return best


def rebuild_evidence_edges(
    conn: sqlite3.Connection,
    *,
    sender_lookup: Optional[Callable[[sqlite3.Connection, set], Dict[str, str]]] = None,
) -> Dict[str, int]:
    """Recompute co_occurrence + communicates_with evidence edges.

    Deletes only the ACTIVE (valid_to IS NULL) evidence edges and rebuilds them;
    part_of and closed history are preserved. Each rebuilt edge is stamped with
    its evidence's provenance (metadata.actor_role + role_mix) so the graph can
    render the personal→ambient attribution spectrum. Returns counts written.

    ``sender_lookup`` is retained for API compatibility with older tests but is
    no longer used for communicates_with (P3.2 co-participation replaces
    sender→mention linking).

    Gate discipline (M2.2): everything expensive — the mention scan, role-map
    computation, participation load, AND the edge fold itself (in-memory via
    :class:`_EdgeAccumulator`) — runs OUTSIDE the write gate. The gate is held
    only for contact seeding and for one DELETE + batched INSERT swap, so
    other writers stall for a bounded swap instead of the whole rebuild (120s
    observed 2026-08-07). The delete and insert share one hold, so readers on
    other connections never observe the edge-less gap between them.
    """
    del sender_lookup  # unused — kept for call-site compatibility
    from ...storage.db.write_gate import with_db_write
    from .edges import EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES, _canonical_order, _now_iso
    from .resolver import EntityResolver

    # Seeding mints person entities the participation load resolves against,
    # so it must land (it commits internally) before the read phase.
    with with_db_write():
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()

    # -- read phase: gate released --------------------------------------------
    rows = conn.execute(
        """
        SELECT record_id, entity_id, canonical_table, event_at, source_id
        FROM entity_mentions
        WHERE record_id IS NOT NULL
        ORDER BY COALESCE(event_at, created_at)
        """
    ).fetchall()

    by_record: Dict[str, Dict] = {}
    for record_id, entity_id, canonical_table, event_at, source_id in rows:
        rec = by_record.setdefault(
            str(record_id),
            {"table": canonical_table, "event_at": event_at, "source_id": source_id, "ents": []},
        )
        rec["ents"].append(str(entity_id))

    role_by_record = _record_role_map(conn, by_record)
    participation = _load_conversation_participation(conn)

    co = 0
    # (src, dst, type) -> {role: evidence_count}; folded into metadata below.
    edge_roles: Dict[tuple, Dict[str, int]] = {}

    def _tally(src: str, dst: str, edge_type: str, role: str) -> None:
        key = _canonical_order(src, dst, edge_type) + (edge_type,)
        mix = edge_roles.setdefault(key, {})
        mix[role] = mix.get(role, 0) + 1

    # -- fold phase: replay every observation in memory, still no gate --------
    acc = _EdgeAccumulator()

    for record_id, rec in by_record.items():
        # Cap per-record fan-out (mirrors the ingest path) so a giant record
        # doesn't create O(n^2) edges.
        unique = list(dict.fromkeys(rec["ents"]))[:8]
        event_at = rec["event_at"]
        role = role_by_record.get(record_id, "ambient")
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                acc.add(unique[i], unique[j], EDGE_CO_OCCURRENCE, event_at)
                _tally(unique[i], unique[j], EDGE_CO_OCCURRENCE, role)
                co += 1

    # P3.2: talked-to edges from thread co-participants only.
    comm = 0
    for src, dst, event_at, role in _participation_edge_events(participation):
        acc.add(src, dst, EDGE_COMMUNICATES, event_at)
        _tally(src, dst, EDGE_COMMUNICATES, role)
        comm += 1

    # -- write phase: swap the folded edge set in under one bounded hold ------
    valid_from = _now_iso()
    payload = []
    for (src, dst, edge_type), state in acc.edges.items():
        mix = edge_roles.get((src, dst, edge_type))
        metadata = (
            json.dumps({"actor_role": _dominant_role(mix), "role_mix": mix})
            if mix
            else None
        )
        payload.append(
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                src,
                dst,
                edge_type,
                float(state["weight"]),
                int(state["count"]),
                state["last"],
                valid_from,
                metadata,
            )
        )

    with with_db_write():
        conn.execute(
            "DELETE FROM entity_edges "
            "WHERE edge_type IN ('co_occurrence', 'communicates_with') AND valid_to IS NULL"
        )
        conn.executemany(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at, valid_from, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        conn.commit()
    return {"co_occurrence": co, "communicates_with": comm}


def compute_communities(conn: sqlite3.Connection) -> Dict[str, int]:
    """Louvain neighborhoods over the active entity graph (the witcher-network
    recipe: community_louvain.best_partition on the weighted co-occurrence
    graph).

    Community ids are re-ranked by size (0 = largest neighborhood) so colors
    stay reasonably stable across rebuilds, and stamped into
    entities.metadata_json.community_id; entities no longer in the graph get
    the stale label removed. random_state pins the partition for determinism.
    """
    try:
        import networkx as nx
    except ImportError:  # pragma: no cover - optional in stripped builds
        logger.warning("networkx unavailable; skipping communities")
        return {"communities": 0, "nodes_labeled": 0}

    G = nx.Graph()
    for src, dst, weight in conn.execute(
        "SELECT src_entity_id, dst_entity_id, weight FROM entity_edges WHERE valid_to IS NULL"
    ):
        if src == dst:
            continue
        w = float(weight or 1.0)
        if G.has_edge(src, dst):
            G[src][dst]["weight"] += w  # parallel edge types accumulate
        else:
            G.add_edge(src, dst, weight=w)

    if G.number_of_nodes() == 0:
        return {"communities": 0, "nodes_labeled": 0}

    # Louvain is pure CPU over the in-memory graph — no reason to hold the
    # write gate (or even a transaction) while it runs.
    community_sets = nx.community.louvain_communities(G, weight="weight", seed=42)
    partition: Dict[str, int] = {}
    sizes: Dict[int, int] = {}
    for comm, members in enumerate(community_sets):
        sizes[comm] = len(members)
        for entity_id in members:
            partition[str(entity_id)] = comm
    rank = {
        comm: i
        for i, (comm, _n) in enumerate(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))
    }

    from ...storage.db.write_gate import with_db_write

    with with_db_write():
        for entity_id, comm in partition.items():
            conn.execute(
                "UPDATE entities SET metadata_json=json_patch(COALESCE(metadata_json,'{}'), ?) "
                "WHERE entity_id=?",
                (json.dumps({"community_id": rank[comm]}), entity_id),
            )
        # Drop stale labels from entities that left the graph.
        placeholders = ",".join("?" for _ in partition) or "''"
        conn.execute(
            f"UPDATE entities SET metadata_json=json_remove(metadata_json, '$.community_id') "
            f"WHERE metadata_json IS NOT NULL "
            f"AND json_extract(metadata_json, '$.community_id') IS NOT NULL "
            f"AND entity_id NOT IN ({placeholders})",
            tuple(partition.keys()),
        )
        conn.commit()
    return {"communities": len(sizes), "nodes_labeled": len(partition)}


def _count_active_edges(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    )


def rebuild_entity_graph(
    conn: sqlite3.Connection,
    *,
    prune_orphans: bool = True,
    refresh: bool = True,
    materialize_facts: bool = True,
) -> Dict[str, object]:
    """Full cheap rebuild of the entity graph from existing derived data.

    Recounts mention totals, optionally prunes mention-orphaned entities,
    rebuilds co-occurrence (from mentions) + communicates_with (from thread
    co-participation), and (by default) materializes facts + topic clusters
    from signal_objects into labeled temporal edges. Refreshes dossiers.
    Returns a before/after report.

    No NER re-run — bounded by the current `entity_mentions` + message
    senders / `conversation_participants` + `signal_objects`. To grow the
    mention set, re-run the `entities` enrichment job (force_reprocess).

    Gate discipline (M2.2): each write phase takes the process-wide write gate
    itself and commits before releasing it; the heavy read/compute work
    (mention scan, role map, participation load, Louvain) runs between holds.
    Callers must NOT wrap this function in ``with_db_write`` — the gate is
    reentrant, so an outer hold silently reinstates the whole-rebuild
    exclusive section (120s observed 2026-08-07) this structure removes.
    """
    from ...storage.db.write_gate import with_db_write
    from ..lifecycle.derived_scrub import _delete_orphan_entities, _recount_entity_mentions

    edges_before = _count_active_edges(conn)

    with with_db_write():
        _recount_entity_mentions(conn)
        orphaned = _delete_orphan_entities(conn) if prune_orphans else []
        conn.commit()

    # Gates its own phases; reads run outside the gate.
    edge_counts = rebuild_evidence_edges(conn)

    # Close facts whose provenance is entirely gone BEFORE materializing, so a
    # dead fact can't re-enter the graph as an edge (the AWS-cert leak).
    from ..lifecycle.derived_scrub import close_dangling_facts

    with with_db_write():
        facts_closed = close_dangling_facts(conn)  # commits internally

    mz = {"topic_edges": 0, "fact_edges": 0}
    enrich = {"goal_edges": 0, "place_edges": 0, "conversation_edges": 0}
    if materialize_facts:
        try:
            from .fact_materializer import materialize_signal_objects_to_graph

            with with_db_write():
                mz = materialize_signal_objects_to_graph(conn)  # commits internally
        except Exception as exc:  # materialization is best-effort
            logger.warning("fact materialization during rebuild failed: %s", exc)
        try:
            from .graph_enrichers import materialize_graph_enrichments

            # After the facts refresh (which drops ALL mz edges) so enricher
            # edges live in the same lifecycle.
            with with_db_write():
                enrich = materialize_graph_enrichments(conn)  # commits internally
        except Exception as exc:
            logger.warning("graph enrichment during rebuild failed: %s", exc)

    # Neighborhoods over the final edge set (evidence + materialized).
    # Gates only its label-write phase; Louvain runs outside the gate.
    communities = compute_communities(conn)

    dossiers = 0
    if refresh:
        try:
            from .dossier import refresh_dossiers

            with with_db_write():
                dossiers = refresh_dossiers(conn)  # commits internally
        except Exception as exc:  # dossier refresh is best-effort
            logger.warning("dossier refresh during rebuild failed: %s", exc)

    edges_after = _count_active_edges(conn)
    return {
        "edges_before": edges_before,
        "edges_after": edges_after,
        "co_occurrence": edge_counts["co_occurrence"],
        "communicates_with": edge_counts["communicates_with"],
        "topic_edges": mz["topic_edges"],
        "fact_edges": mz["fact_edges"],
        "goal_edges": enrich["goal_edges"],
        "place_edges": enrich["place_edges"],
        "conversation_edges": enrich["conversation_edges"],
        "orphans_pruned": len(orphaned),
        "facts_closed_dangling": facts_closed,
        "communities": communities["communities"],
        "dossiers_refreshed": dossiers,
    }
