"""Entity-graph maintenance: rebuild evidence edges from surviving mentions.

Entity edges (co_occurrence, communicates_with) are written incrementally at
ingest time by EntitiesJob. There is no ingest-independent way to recompute
them, so:

  * after a source deletion the decayed weights can't subtract removed evidence
    (lifecycle/derived_scrub calls in here), and
  * if extraction was disabled/partial for a stretch, the graph under-counts.

`rebuild_evidence_edges` recomputes both evidence edge types deterministically
from the `entity_mentions` table (the resolved, deduped record↔entity links),
which is cheap at personal scale and needs no NER re-run. `part_of` edges are
structural (derived from entity names, not mention evidence) and are left
untouched; closed/superseded history rows are preserved.

Note the historical bug this fixes: the previous rebuild deleted
`communicates_with` edges but only recreated `co_occurrence`, so every
source-scrub silently and permanently dropped every sender→entity edge.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Callable, Dict, Optional

logger = logging.getLogger("topos.features.entities.maintenance")

# Canonical message tables that carry a sender we can attribute a
# communicates_with edge to. Maps table -> sender column name.
_SENDER_TABLES = {
    "conversation_messages": "sender_id",
    "ai_chat_messages": "sender_id",
    "conversation_message": "sender_id",
}


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
    """Recompute co_occurrence + communicates_with edges from surviving mentions.

    Deletes only the ACTIVE (valid_to IS NULL) evidence edges and rebuilds them;
    part_of and closed history are preserved. Each rebuilt edge is stamped with
    its evidence's provenance (metadata.actor_role + role_mix) so the graph can
    render the personal→ambient attribution spectrum. Returns counts written.
    """
    from .edges import EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES, _canonical_order, update_edge
    from .resolver import EntityResolver

    resolver = EntityResolver(conn)
    resolver.seed_from_contacts()

    conn.execute(
        "DELETE FROM entity_edges "
        "WHERE edge_type IN ('co_occurrence', 'communicates_with') AND valid_to IS NULL"
    )

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

    tables = {r["table"] for r in by_record.values() if r["table"]}
    lookup = sender_lookup or _load_senders
    sender_by_record = lookup(conn, tables)
    role_by_record = _record_role_map(conn, by_record)

    co = comm = 0
    # (src, dst, type) -> {role: evidence_count}; stamped onto edges afterwards.
    edge_roles: Dict[tuple, Dict[str, int]] = {}

    def _tally(src: str, dst: str, edge_type: str, role: str) -> None:
        key = _canonical_order(src, dst, edge_type) + (edge_type,)
        mix = edge_roles.setdefault(key, {})
        mix[role] = mix.get(role, 0) + 1

    for record_id, rec in by_record.items():
        # Cap per-record fan-out (mirrors the ingest path) so a giant record
        # doesn't create O(n^2) edges.
        unique = list(dict.fromkeys(rec["ents"]))[:8]
        event_at = rec["event_at"]
        role = role_by_record.get(record_id, "ambient")
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                update_edge(
                    conn,
                    src_entity_id=unique[i],
                    dst_entity_id=unique[j],
                    edge_type=EDGE_CO_OCCURRENCE,
                    event_at=event_at,
                )
                _tally(unique[i], unique[j], EDGE_CO_OCCURRENCE, role)
                co += 1
        sender_raw = sender_by_record.get(record_id)
        if sender_raw:
            try:
                sender_id, _tier = resolver.resolve(sender_raw, entity_type="person")
            except ValueError:
                sender_id = None
            if sender_id:
                for ent in unique:
                    if ent == sender_id:
                        continue
                    update_edge(
                        conn,
                        src_entity_id=sender_id,
                        dst_entity_id=ent,
                        edge_type=EDGE_COMMUNICATES,
                        event_at=event_at,
                    )
                    _tally(sender_id, ent, EDGE_COMMUNICATES, role)
                    comm += 1

    # Stamp the aggregated provenance onto each active edge's metadata.
    for (src, dst, edge_type), mix in edge_roles.items():
        conn.execute(
            """
            UPDATE entity_edges
            SET metadata_json = json_patch(COALESCE(metadata_json, '{}'), ?)
            WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
            """,
            (json.dumps({"actor_role": _dominant_role(mix), "role_mix": mix}), src, dst, edge_type),
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
    rebuilds co-occurrence/communicates evidence edges from mentions, and
    (by default) materializes facts + topic clusters from signal_objects into
    labeled temporal edges. Refreshes dossiers. Returns a before/after report.

    No NER re-run — bounded by the current `entity_mentions` + `signal_objects`.
    To grow the mention set, re-run the `entities` enrichment job
    (force_reprocess).
    """
    from ..lifecycle.derived_scrub import _delete_orphan_entities, _recount_entity_mentions

    edges_before = _count_active_edges(conn)

    _recount_entity_mentions(conn)
    orphaned = _delete_orphan_entities(conn) if prune_orphans else []
    edge_counts = rebuild_evidence_edges(conn)

    # Close facts whose provenance is entirely gone BEFORE materializing, so a
    # dead fact can't re-enter the graph as an edge (the AWS-cert leak).
    from ..lifecycle.derived_scrub import close_dangling_facts

    facts_closed = close_dangling_facts(conn)

    mz = {"topic_edges": 0, "fact_edges": 0}
    enrich = {"goal_edges": 0, "place_edges": 0, "conversation_edges": 0}
    if materialize_facts:
        try:
            from .fact_materializer import materialize_signal_objects_to_graph

            mz = materialize_signal_objects_to_graph(conn)
        except Exception as exc:  # materialization is best-effort
            logger.warning("fact materialization during rebuild failed: %s", exc)
        try:
            from .graph_enrichers import materialize_graph_enrichments

            # After the facts refresh (which drops ALL mz edges) so enricher
            # edges live in the same lifecycle.
            enrich = materialize_graph_enrichments(conn)
        except Exception as exc:
            logger.warning("graph enrichment during rebuild failed: %s", exc)

    # Neighborhoods over the final edge set (evidence + materialized).
    communities = compute_communities(conn)

    dossiers = 0
    if refresh:
        try:
            from .dossier import refresh_dossiers

            dossiers = refresh_dossiers(conn)
        except Exception as exc:  # dossier refresh is best-effort
            logger.warning("dossier refresh during rebuild failed: %s", exc)
    conn.commit()

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
