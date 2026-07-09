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


def rebuild_evidence_edges(
    conn: sqlite3.Connection,
    *,
    sender_lookup: Optional[Callable[[sqlite3.Connection, set], Dict[str, str]]] = None,
) -> Dict[str, int]:
    """Recompute co_occurrence + communicates_with edges from surviving mentions.

    Deletes only the ACTIVE (valid_to IS NULL) evidence edges and rebuilds them;
    part_of and closed history are preserved. Returns counts of edges written.
    """
    from .edges import EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES, update_edge
    from .resolver import EntityResolver

    resolver = EntityResolver(conn)
    resolver.seed_from_contacts()

    conn.execute(
        "DELETE FROM entity_edges "
        "WHERE edge_type IN ('co_occurrence', 'communicates_with') AND valid_to IS NULL"
    )

    rows = conn.execute(
        """
        SELECT record_id, entity_id, canonical_table, event_at
        FROM entity_mentions
        WHERE record_id IS NOT NULL
        ORDER BY COALESCE(event_at, created_at)
        """
    ).fetchall()

    by_record: Dict[str, Dict] = {}
    for record_id, entity_id, canonical_table, event_at in rows:
        rec = by_record.setdefault(
            str(record_id),
            {"table": canonical_table, "event_at": event_at, "ents": []},
        )
        rec["ents"].append(str(entity_id))

    tables = {r["table"] for r in by_record.values() if r["table"]}
    lookup = sender_lookup or _load_senders
    sender_by_record = lookup(conn, tables)

    co = comm = 0
    for record_id, rec in by_record.items():
        # Cap per-record fan-out (mirrors the ingest path) so a giant record
        # doesn't create O(n^2) edges.
        unique = list(dict.fromkeys(rec["ents"]))[:8]
        event_at = rec["event_at"]
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                update_edge(
                    conn,
                    src_entity_id=unique[i],
                    dst_entity_id=unique[j],
                    edge_type=EDGE_CO_OCCURRENCE,
                    event_at=event_at,
                )
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
                    comm += 1

    conn.commit()
    return {"co_occurrence": co, "communicates_with": comm}


def _count_active_edges(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM entity_edges WHERE valid_to IS NULL").fetchone()[0]
    )


def rebuild_entity_graph(
    conn: sqlite3.Connection,
    *,
    prune_orphans: bool = True,
    refresh: bool = True,
) -> Dict[str, object]:
    """Full cheap rebuild of the entity graph from existing mentions.

    Recounts mention totals, optionally prunes mention-orphaned entities,
    rebuilds evidence edges, and refreshes dossiers. Returns a before/after
    report. No NER re-run — bounded by the current `entity_mentions` set. To
    grow the mention set, re-run the `entities` enrichment job (force_reprocess).
    """
    from ..lifecycle.derived_scrub import _delete_orphan_entities, _recount_entity_mentions

    edges_before = _count_active_edges(conn)

    _recount_entity_mentions(conn)
    orphaned = _delete_orphan_entities(conn) if prune_orphans else []
    edge_counts = rebuild_evidence_edges(conn)

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
        "orphans_pruned": len(orphaned),
        "dossiers_refreshed": dossiers,
    }
