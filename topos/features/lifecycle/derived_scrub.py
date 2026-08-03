"""Propagate source/record deletion into the derived-intelligence layers.

The generic attribution scrub (sources/scrub_attribution.py) already deletes
rows in any table carrying a source_id column — that covers entity_mentions,
timeline, signal_embeddings (+ ANN rows), and cluster tables. What it cannot
cover, by construction, are the layers where per-source contribution is not a
row you can delete:

  * stat_state       — Welford/histogram states are non-subtractable aggregates;
                       the only correct removal is wipe + refold from remaining
                       canonical rows (measured ~2s for a 46k-row corpus)
  * entity registry  — mention counts go stale, mention-orphaned entities
                       linger, decayed edge weights can't subtract evidence;
                       edges are rebuilt from remaining mentions
  * dossiers         — summarize scrubbed content until refreshed
  * facts            — provenance lives in source_refs, not a source_id column
  * embedding_entities / stat_seen / fact_conflicts — orphan bookkeeping

Everything here is deterministic recompute-from-remainder: cheap at personal
scale and exact (no residue), unlike attempting inverse updates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("topos.features.lifecycle.derived_scrub")


# ---------------------------------------------------------------- entities


def _recount_entity_mentions(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE entities SET mention_count = (
            SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = entities.entity_id
        )
        """
    )
    return int(cursor.rowcount or 0)


def _delete_entity_cascade(conn: sqlite3.Connection, entity_id: str) -> Dict[str, int]:
    """Hard-delete one entity and its mention/edge/vector/review footprint.

    Closes the open dossier (valid_to) rather than deleting the signal_object
    row — same provenance-preserving choice as orphan prune.
    """
    counts = {"mentions": 0, "edges": 0, "vectors": 0, "reviews": 0, "dossiers_closed": 0}
    try:
        cur = conn.execute(
            "DELETE FROM entity_mentions WHERE entity_id=?", (entity_id,)
        )
        counts["mentions"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "DELETE FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
            (entity_id, entity_id),
        )
        counts["edges"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "DELETE FROM entity_context_vectors WHERE entity_id=?", (entity_id,)
        )
        counts["vectors"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "DELETE FROM entity_review WHERE candidate_entity_id=?", (entity_id,)
        )
        counts["reviews"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        pass
    try:
        cur = conn.execute(
            "UPDATE signal_objects SET valid_to=datetime('now') "
            "WHERE object_type='entity_dossier' AND object_key=? AND valid_to IS NULL",
            (f"dossier:{entity_id}",),
        )
        counts["dossiers_closed"] = int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM entities WHERE entity_id=?", (entity_id,))
    return counts


def _delete_orphan_entities(conn: sqlite3.Connection) -> List[str]:
    """Entities with no remaining mentions and no LIVE contact anchor are removed.

    Contact-seeded entities stay even at zero mentions — the contact row is
    their provenance, and the canonical contacts table has its own scrub path.
    But the anchor must still RESOLVE: when the contact row itself was deleted
    (source scrub, manual cleanup), the anchored entity would otherwise persist
    forever with a dangling contact_id (2026-07-09 demo-purge leak).

    Materialized graph hubs (goals / topic_* / conversations) also stay: they
    are derived from ``user_goals`` / ``signal_objects`` / conversation rows,
    never from ``entity_mentions``, so mention_count is always 0. Pruning them
    here used to wipe the goal layer on every rebuild until enrichers ran —
    and permanently if enrich failed after the mz-edge wipe.
    """
    # Synthetic / enrichment-only nodes — never mention-backed.
    keep_clause = (
        "AND entity_type NOT IN ('goal', 'conversation') "
        "AND entity_id NOT LIKE 'goal_%' "
        "AND entity_id NOT LIKE 'topic_%' "
        "AND entity_id NOT LIKE 'conv_%'"
    )
    try:
        rows = conn.execute(
            f"""
            SELECT entity_id FROM entities e
            WHERE mention_count = 0 AND is_self = 0
              AND (contact_id IS NULL
                   OR NOT EXISTS (SELECT 1 FROM contacts c WHERE c.contact_id = e.contact_id))
              {keep_clause}
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # No contacts table in this database — fall back to the anchor-blind rule.
        rows = conn.execute(
            f"""
            SELECT entity_id FROM entities
            WHERE mention_count = 0 AND contact_id IS NULL AND is_self = 0
              {keep_clause}
            """
        ).fetchall()
    orphan_ids = [str(r[0]) for r in rows]
    for entity_id in orphan_ids:
        _delete_entity_cascade(conn, entity_id)
    return orphan_ids


def purge_junk_minted_entities(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> Dict[str, Any]:
    """One-shot C4 residual scrub: remove already-minted junk spine entities.

    Uses the same ``is_valid_entity_surface`` predicate as the mint filter so
    allowlisted short names (AWS, Max, C3) survive. Synthetic hubs and the
    self entity are never candidates. After apply, evidence edges are rebuilt
    from surviving mentions.
    """
    from ..entities.resolver import is_valid_entity_surface

    report: Dict[str, Any] = {
        "junk_entities_found": 0,
        "junk_entities_removed": 0,
        "mentions_removed": 0,
        "edges_removed": 0,
        "dry_run": bool(dry_run),
        "samples": [],
    }
    if not _table_exists(conn, "entities"):
        return report

    keep_clause = (
        "entity_type NOT IN ('goal', 'conversation') "
        "AND entity_id NOT LIKE 'goal_%' "
        "AND entity_id NOT LIKE 'topic_%' "
        "AND entity_id NOT LIKE 'conv_%' "
        "AND is_self = 0"
    )
    try:
        rows = conn.execute(
            f"SELECT entity_id, canonical_name FROM entities WHERE {keep_clause}"
        ).fetchall()
    except sqlite3.OperationalError:
        return report

    junk: List[tuple] = []
    for entity_id, canonical_name in rows:
        if not is_valid_entity_surface(str(canonical_name or "")):
            junk.append((str(entity_id), str(canonical_name or "")))

    report["junk_entities_found"] = len(junk)
    report["samples"] = [
        {"entity_id": eid, "canonical_name": name} for eid, name in junk[:20]
    ]
    if dry_run or not junk:
        return report

    for entity_id, _name in junk:
        counts = _delete_entity_cascade(conn, entity_id)
        report["junk_entities_removed"] += 1
        report["mentions_removed"] += counts["mentions"]
        report["edges_removed"] += counts["edges"]

    from ..entities.maintenance import rebuild_evidence_edges

    rebuilt = rebuild_evidence_edges(conn)
    report["edges_rebuilt"] = rebuilt
    conn.commit()
    logger.info(
        "C4 scrub: removed %d junk entities (%d mentions, %d edges)",
        report["junk_entities_removed"],
        report["mentions_removed"],
        report["edges_removed"],
    )
    return report


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _rebuild_entity_edges(conn: sqlite3.Connection) -> int:
    """Recompute evidence edges from remaining mentions.

    Decayed weights cannot subtract removed evidence, so co_occurrence and
    communicates_with are rebuilt from surviving mentions. part_of edges are
    structural (derived from names, not scrubbed evidence) and are kept for
    surviving entities.

    Delegates to the shared entity-graph rebuild so co_occurrence AND
    communicates_with are recreated — previously this deleted communicates_with
    but only rebuilt co_occurrence, silently dropping every sender→entity edge
    on each scrub.
    """
    from ..entities.maintenance import rebuild_evidence_edges

    counts = rebuild_evidence_edges(conn)
    return int(counts.get("co_occurrence", 0) + counts.get("communicates_with", 0))


# ------------------------------------------------------------------ stats


def refold_statistics(conn: sqlite3.Connection) -> Dict[str, int]:
    """Wipe aggregate states and refold from the remaining canonical rows.

    stat_seen must be cleared together with stat_state or the refold would
    skip every remaining record as already-counted.
    """
    from ..stats.engine import StatsEngine

    conn.execute("DELETE FROM stat_state")
    conn.execute("DELETE FROM stat_seen")
    conn.commit()

    engine = StatsEngine(conn)
    folded = 0
    tables = (
        "ai_chat_messages",
        "conversation_messages",
        "journal_entries",
        "calendar_events",
        "profile_records",
        "activity_events",
        "financial_transactions",
        "location_events",
    )
    previous_factory = conn.row_factory
    try:
        for table in tables:
            try:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
            except sqlite3.OperationalError:
                continue
            for row in rows:
                row["_table"] = table
            for start in range(0, len(rows), 500):
                folded += engine.fold_batch(rows[start : start + 500])["rows_folded"]
    finally:
        # Never leave the shared engine connection without Row factory.
        conn.row_factory = previous_factory if previous_factory is not None else sqlite3.Row
    return {"rows_refolded": folded}


def repromote_stat_insights(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-render insight facts and prune ones whose groups no longer exist."""
    from ...storage.adapters.factory import AdapterFactory
    from ..stats.engine import StatsEngine

    engine = StatsEngine(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    written = engine.promote_insights(bundle)

    # Prune stale insight facts (their stat group vanished with the source).
    live_ids: Set[str] = set()
    from ..stats.insights import render_insights

    for insight in render_insights(engine):
        live_ids.add(f"stat:{insight['stat_id']}:{insight.get('group_key') or 'all'}")
    rows = conn.execute(
        "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'stat:%'"
    ).fetchall()
    pruned = 0
    for (fact_id,) in rows:
        if str(fact_id) not in live_ids:
            conn.execute("DELETE FROM signal_facts WHERE fact_id=?", (fact_id,))
            pruned += 1
    conn.commit()
    return {"insights_written": written, "insights_pruned": pruned}


# ------------------------------------------------------------------ facts


def _record_source_map(conn: sqlite3.Connection, record_ids: Set[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    ids = [r for r in record_ids if r]
    for start in range(0, len(ids), 200):
        chunk = ids[start : start + 200]
        placeholders = ",".join("?" for _ in chunk)
        try:
            for record_id, source_id in conn.execute(
                f"SELECT record_id, source_id FROM timeline WHERE record_id IN ({placeholders})",
                chunk,
            ).fetchall():
                if source_id:
                    out[str(record_id)] = str(source_id)
        except sqlite3.OperationalError:
            break
    return out


def purge_facts_for_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    scrubbed_record_ids: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Hard-delete facts whose evidence came entirely from the scrubbed source.

    Facts with mixed provenance survive with the scrubbed refs removed —
    the remaining sources still attest the claim. Scrub semantics are hard
    deletion (unlike owner exclusions, which soft-close and tombstone).
    """
    rows = conn.execute(
        """
        SELECT object_id, source_refs_json FROM signal_objects
        WHERE object_type='fact'
        """
    ).fetchall()

    all_ref_ids: Set[str] = set()
    parsed: List[tuple] = []
    for object_id, refs_json in rows:
        try:
            refs = json.loads(refs_json or "[]")
        except json.JSONDecodeError:
            refs = []
        parsed.append((str(object_id), refs))
        for ref in refs:
            if isinstance(ref, dict) and ref.get("record_id"):
                all_ref_ids.add(str(ref["record_id"]))

    source_by_record = _record_source_map(conn, all_ref_ids)
    scrubbed_records = scrubbed_record_ids or set()

    def _ref_is_scrubbed(ref: Dict[str, Any]) -> bool:
        record_id = str(ref.get("record_id") or "")
        if ref.get("source_id"):
            return str(ref["source_id"]) == source_id
        if record_id in scrubbed_records:
            return True
        # Legacy refs without source_id: a record absent from the source map
        # AND absent from canonical tables was deleted by the attribution
        # sweep — treat as scrubbed when it can no longer be attributed.
        return source_by_record.get(record_id) == source_id

    deleted = 0
    trimmed = 0
    for object_id, refs in parsed:
        if not refs:
            continue
        scrubbed = [r for r in refs if isinstance(r, dict) and _ref_is_scrubbed(r)]
        if not scrubbed:
            continue
        surviving = [r for r in refs if not (isinstance(r, dict) and _ref_is_scrubbed(r))]
        if surviving:
            conn.execute(
                "UPDATE signal_objects SET source_refs_json=?, updated_at=datetime('now') WHERE object_id=?",
                (json.dumps(surviving), object_id),
            )
            trimmed += 1
        else:
            conn.execute("DELETE FROM signal_objects WHERE object_id=?", (object_id,))
            deleted += 1
    conn.commit()
    return {"facts_deleted": deleted, "facts_trimmed": trimmed}


_REF_ID_COLUMNS = ("record_id", "message_id", "entry_id", "event_id", "transaction_id", "id")


def _ref_record_exists(conn: sqlite3.Connection, table: str, record_id: str) -> Optional[bool]:
    """Does the referenced record still exist? None = unverifiable (be conservative)."""
    if not table.replace("_", "").isalnum():
        return None  # suspicious table name — never interpolate it
    try:
        cols = [str(c[1]) for c in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return None
    if not cols:
        return False  # the whole table was dropped (raw retention scrub)
    id_col = next((c for c in _REF_ID_COLUMNS if c in cols), None)
    if id_col is None:
        return None  # no recognisable id column — can't verify
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {id_col}=? LIMIT 1", (record_id,)
    ).fetchone()
    return row is not None


def close_dangling_facts(conn: sqlite3.Connection) -> int:
    """Close active facts whose every source_ref points at a deleted record.

    purge_facts_for_source only removes refs attributable to the scrubbed
    source; legacy refs without a source_id can keep a fact alive after its
    evidence is gone (the "certified_in AWS Solutions Architect" leak). This
    sweep CLOSES such facts (valid_to stamped, row kept) rather than deleting —
    provenance may return, and history stays auditable. Refs that cannot be
    verified (no table/record_id, unknown schema) count as alive: closing a
    real fact is worse than keeping a stale one an extra sweep.
    """
    rows = conn.execute(
        """
        SELECT object_id, source_refs_json FROM signal_objects
        WHERE object_type='fact' AND valid_to IS NULL
        """
    ).fetchall()
    closed = 0
    for object_id, refs_json in rows:
        try:
            refs = [r for r in json.loads(refs_json or "[]") if isinstance(r, dict)]
        except json.JSONDecodeError:
            continue
        if not refs:
            continue
        any_alive = False
        for ref in refs:
            # Source-attributed refs are the attribution sweep's jurisdiction:
            # purge_facts_for_source trims them when THEIR source scrubs, so a
            # still-present attributed ref counts as live evidence. This sweep
            # only judges legacy refs without a source_id — the kind that kept
            # the AWS-cert fact alive after its record was deleted.
            if ref.get("source_id"):
                any_alive = True
                break
            table = str(ref.get("table") or "")
            record_id = str(ref.get("record_id") or "")
            if not table or not record_id:
                any_alive = True  # unverifiable → conservative
                break
            # timeline is the record registry (the attribution sweep deletes its
            # rows with the source) — presence there means the record lives even
            # if the canonical row isn't materialized (test corpora, lean nodes).
            try:
                in_timeline = conn.execute(
                    "SELECT 1 FROM timeline WHERE record_id=? LIMIT 1", (record_id,)
                ).fetchone() is not None
            except sqlite3.OperationalError:
                in_timeline = False
            if in_timeline:
                any_alive = True
                break
            exists = _ref_record_exists(conn, table, record_id)
            if exists is None or exists:
                any_alive = True
                break
        if not any_alive:
            conn.execute(
                "UPDATE signal_objects SET valid_to=datetime('now'), updated_at=datetime('now') "
                "WHERE object_id=?",
                (object_id,),
            )
            closed += 1
    conn.commit()
    return closed


# ----------------------------------------------------------------- orphans


def sweep_orphans(conn: sqlite3.Connection) -> Dict[str, int]:
    out: Dict[str, int] = {}
    cursor = conn.execute(
        """
        DELETE FROM embedding_entities WHERE embedding_id NOT IN (
            SELECT embedding_id FROM signal_embeddings
        )
        """
    )
    out["embedding_entities"] = int(cursor.rowcount or 0)
    cursor = conn.execute(
        """
        DELETE FROM fact_conflicts WHERE incumbent_object_id NOT IN (
            SELECT object_id FROM signal_objects WHERE object_type='fact'
        )
        """
    )
    out["fact_conflicts"] = int(cursor.rowcount or 0)
    cursor = conn.execute(
        "DELETE FROM entity_review WHERE candidate_entity_id NOT IN (SELECT entity_id FROM entities)"
    )
    out["entity_review"] = int(cursor.rowcount or 0)
    return out


# -------------------------------------------------------------- entrypoints


def purge_derived_for_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    scrubbed_record_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Run after the attribution sweep deleted the source's attributed rows.

    Order matters: mentions are already gone (source_id sweep), so recount ->
    orphan removal -> edge rebuild -> stats refold -> insight re-promotion ->
    fact purge -> dossier refresh -> orphan sweep.
    """
    report: Dict[str, Any] = {"source_id": source_id}

    report["entities_recounted"] = _recount_entity_mentions(conn)
    orphans = _delete_orphan_entities(conn)
    report["entities_removed"] = len(orphans)
    report["edges_rebuilt"] = _rebuild_entity_edges(conn)
    conn.commit()

    report.update(refold_statistics(conn))
    report.update(repromote_stat_insights(conn))
    report.update(purge_facts_for_source(conn, source_id, scrubbed_record_ids=scrubbed_record_ids))
    # Catch facts the source-attribution purge couldn't attribute (legacy refs
    # without source_id): anything whose evidence is now entirely gone closes.
    report["facts_closed_dangling"] = close_dangling_facts(conn)

    try:
        from ..entities.dossier import refresh_dossiers

        report["dossiers_refreshed"] = refresh_dossiers(conn)
    except Exception as exc:  # noqa: BLE001
        report["dossiers_refreshed"] = f"failed: {exc}"

    report["orphans"] = sweep_orphans(conn)
    conn.commit()
    return report


def purge_derived_for_records(
    conn: sqlite3.Connection,
    record_ids: List[str],
) -> Dict[str, Any]:
    """Record-level removal: delete everything derived from specific records.

    Used by the owner 'remove this from my intelligence' flow. Canonical row
    deletion is the caller's concern; this handles the derived layers plus
    embeddings/ANN for the records.
    """
    ids = [str(r) for r in record_ids if str(r).strip()]
    if not ids:
        return {"records": 0}
    report: Dict[str, Any] = {"records": len(ids)}
    placeholders = ",".join("?" for _ in ids)

    embedding_ids = [
        str(r[0])
        for r in conn.execute(
            f"SELECT embedding_id FROM signal_embeddings WHERE record_id IN ({placeholders})",
            ids,
        ).fetchall()
    ]
    conn.execute(f"DELETE FROM signal_embeddings WHERE record_id IN ({placeholders})", ids)
    if embedding_ids:
        from ...storage.adapters.sqlite.stores import SQLiteVectorIndex

        SQLiteVectorIndex(conn).delete_embeddings(embedding_ids)
    report["embeddings_removed"] = len(embedding_ids)

    for table, column in (
        ("entity_mentions", "record_id"),
        ("timeline", "record_id"),
        ("topic_cluster_members", "record_id"),
        ("cluster_candidates", "record_id"),
    ):
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids
        )
        report[table] = int(cursor.rowcount or 0)

    report["entities_recounted"] = _recount_entity_mentions(conn)
    report["entities_removed"] = len(_delete_orphan_entities(conn))
    report["edges_rebuilt"] = _rebuild_entity_edges(conn)
    conn.commit()

    # Aggregates: same non-subtractability as source scrub -> refold.
    report.update(refold_statistics(conn))
    report.update(repromote_stat_insights(conn))

    # Facts evidenced only by these records are removed.
    id_set = set(ids)
    rows = conn.execute(
        "SELECT object_id, source_refs_json FROM signal_objects WHERE object_type='fact'"
    ).fetchall()
    facts_deleted = 0
    facts_trimmed = 0
    for object_id, refs_json in rows:
        try:
            refs = json.loads(refs_json or "[]")
        except json.JSONDecodeError:
            refs = []
        if not refs:
            continue
        surviving = [
            r for r in refs
            if not (isinstance(r, dict) and str(r.get("record_id") or "") in id_set)
        ]
        if len(surviving) == len(refs):
            continue
        if surviving:
            conn.execute(
                "UPDATE signal_objects SET source_refs_json=?, updated_at=datetime('now') WHERE object_id=?",
                (json.dumps(surviving), str(object_id)),
            )
            facts_trimmed += 1
        else:
            conn.execute("DELETE FROM signal_objects WHERE object_id=?", (str(object_id),))
            facts_deleted += 1
    report["facts_deleted"] = facts_deleted
    report["facts_trimmed"] = facts_trimmed

    try:
        from ..entities.dossier import refresh_dossiers

        report["dossiers_refreshed"] = refresh_dossiers(conn)
    except Exception as exc:  # noqa: BLE001
        report["dossiers_refreshed"] = f"failed: {exc}"
    report["orphans"] = sweep_orphans(conn)
    conn.commit()
    return report
