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

from ...storage.db.write_gate import batched_writes, commit_connection, with_db_write

logger = logging.getLogger("topos.features.lifecycle.derived_scrub")


# ------------------------------------------------------- protected entities


def _protected_entity_keys(conn: sqlite3.Connection) -> tuple:
    """``(entity_ids, normalized_names)`` a scrub may never reap.

    A black-holed entity must survive its own housekeeping. ``BlackholeGuard``
    resolves the *exact* half of its filter from the entity id — both
    ``blocked_record_ids()`` (joining ``entity_mentions``) and
    ``sql_exclusion()`` — so deleting the row silently empties that half and
    degrades a hard protection to the read-time substring name scan. Nothing in
    this module used to know the black-hole tables existed, and the reap chain
    is quiet: mentions removed -> ``mention_count`` hits 0 -> the next
    ``rebuild_evidence_edges`` drops the entity's co-occurrence edges -> the
    next orphan sweep deletes the entity.

    Measured on the live node 2026-08-27: ``Old Harbor- Rey's Place`` carried
    ``rebuild_state='complete'`` while its ``entities`` row was gone, and the
    protected name still sat in ``journal_entries.place_name``,
    ``location_events.place_name``, ``grow_journal_sessions.location``, the
    embedding preview, the search text, and the FTS index.

    Both keys are returned because they protect different things. The id covers
    an entity the flag is bound to. The normalized name covers the two cases the
    id cannot: a re-mint under a fresh id after a reap already happened, and
    ``BlackholeStore``'s pre-emptive protection of a name that has no entity
    yet (``bind_entity_id``).

    Fails OPEN on a missing table only — a database with no black-hole schema
    has nothing to protect. Every other error RAISES: an unreadable protection
    list must stop the scrub, because the alternative is proceeding on the
    assumption that nothing is protected, which is the exact failure this
    function exists to prevent.
    """
    ids: Set[str] = set()
    names: Set[str] = set()
    if not _table_exists(conn, "entity_blackholes"):
        return ids, names
    from .blackhole import BlackholeStore

    store = BlackholeStore(conn)
    try:
        ids = set(store.blackholed_entity_ids())
        # blackholed_name_terms() folds in aliases_json. Reading normalized_name
        # alone made the scrub protect a strict SUBSET of what the guard
        # protects, so an entity minted under a protected ALIAS was reapable by
        # every path this module owns while the guard was still filtering it.
        names = set(store.blackholed_name_terms())
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.error(
            "scrub: could not read entity_blackholes (%s) — refusing to reap any entity",
            exc,
        )
        raise
    return ids, names


def is_entity_protected(
    conn: sqlite3.Connection, entity_id: str, normalized_name: Optional[str] = None
) -> bool:
    """Public predicate for writers OUTSIDE this module that delete entities.

    ``_delete_entity_cascade`` was described as "the one door every entity
    deletion passes through". That was wrong, and an adversarial review found
    four more doors, two of which reap a protected entity in the same pass this
    module refuses to:

      * ``fact_materializer.materialize_signal_objects_to_graph`` — its own
        value-surface purge, which ``maintenance.rebuild_entity_graph`` calls two
        steps AFTER the guarded orphan sweep;
      * ``ExclusionStore.exclude_entity`` — deletes the entity and every mention
        while deliberately leaving the canonical rows, which is precisely the
        shape that empties ``blocked_record_ids()`` and serves the records;
      * ``EntityResolver.merge_entities`` — rebinds the flag onto the surviving
        unrelated entity;
      * ``consolidation.split_surface`` — moves alias mentions to a fresh
        unprotected entity.

    Each of those now calls this. Anything new that deletes from ``entities`` or
    repoints ``entity_mentions`` must call it too; the bhlr gate has a discovery
    test that fails on an unguarded ``DELETE FROM entities``.
    """
    return _is_protected(conn, entity_id, normalized_name)


def _is_protected(
    conn: sqlite3.Connection,
    entity_id: str,
    normalized_name: Optional[str] = None,
    *,
    protected: Optional[tuple] = None,
) -> bool:
    """True when this entity is black-holed and must not be deleted.

    ``protected`` lets a caller hoist the lookup out of a loop; omitting it
    resolves per call. The default is deliberately the safe one — a future
    caller that forgets to pass the set still gets the protection.
    """
    ids, names = protected if protected is not None else _protected_entity_keys(conn)
    if not ids and not names:
        return False
    if str(entity_id) in ids:
        return True
    if normalized_name and str(normalized_name) in names:
        return True
    return False


# ---------------------------------------------------------------- entities


def _recount_entity_mentions(conn: sqlite3.Connection) -> int:
    """Recompute mention_count AND the observation window from surviving mentions.

    The window was fiction at both ends. ``_create_entity`` stamped both
    ``first_seen`` and ``last_seen`` with ``datetime('now')`` at mint time, and
    ``record_mention`` only ever advanced ``last_seen`` upward — so ``first_seen``
    meant "when extraction happened to reach this entity" and ``last_seen`` sat
    ahead of the newest real mention. Measured on the owner's node 2026-08-27, of
    989 mentioned entities: ``first_seen`` was late for **835** (worst by 1,191
    days — ``plurigrid``, first mentioned 2023-04-11, stamped 2026-07-15) and
    ``last_seen`` was ahead of the latest mention for **699**.

    Every surface reads it: the dossier handed to the LLM, the graph node
    property exposed to queries, the API. "Entities first seen before 2024"
    returned nothing on a node holding three years of history.

    ``record_mention`` now lowers ``first_seen`` as it raises ``last_seen``, which
    keeps new writes honest. This is the repair for everything already stored, and
    it belongs here because this function already rebuilds count-from-mentions —
    so every scrub and rebuild corrects the window as a side effect, with no
    separate migration to remember to run.

    Entities with NO mentions are left alone. A materialized graph hub (goal,
    topic, conversation) is a vertex, not a sighting; it has no observation window
    and inventing one from nothing would be a different lie.
    """
    cursor = conn.execute(
        """
        UPDATE entities SET mention_count = (
            SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = entities.entity_id
        )
        """
    )
    recounted = int(cursor.rowcount or 0)
    try:
        conn.execute(
            """
            UPDATE entities SET
                first_seen = COALESCE((
                    SELECT MIN(NULLIF(COALESCE(m.event_at, m.created_at), ''))
                    FROM entity_mentions m WHERE m.entity_id = entities.entity_id
                ), first_seen),
                last_seen = COALESCE((
                    SELECT MAX(NULLIF(COALESCE(m.event_at, m.created_at), ''))
                    FROM entity_mentions m WHERE m.entity_id = entities.entity_id
                ), last_seen)
            WHERE EXISTS (
                SELECT 1 FROM entity_mentions m2 WHERE m2.entity_id = entities.entity_id
            )
            """
        )
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.debug("observation-window repair skipped: %s", exc)
    return recounted


def ref_record_key(ref: Any) -> Optional[tuple]:
    """``(table, record_id)`` a provenance ref points at, whatever key it used.

    THE single reader, because there were several and they all read one key.
    Producers disagree: ``facts/extract.py``, ``facts/llm_extract.py``,
    ``derivation/surfaces.py`` and ``entities/dossier.py`` write
    ``{"table":…, "record_id":…}``, while ``signal/typed_stores`` and the
    derivation extraction path write ``{"table":…, "id":…}``. Both are legitimate
    and neither is going away on its own.

    Every sweep read ``record_id`` only. Measured on the owner's node 2026-08-27
    across 4,577 active objects: **4,229 refs key on ``id`` and 307 on
    ``record_id``**, so the sweeps saw 7% of the provenance in the database and
    silently treated the rest as unattributable.

    A ``day``-keyed ref (303 of them) is deliberately NOT a record key — those are
    day-scoped aggregates citing a date, not a row — so this returns None for them
    rather than inventing a record id that resolves to nothing.
    """
    if not isinstance(ref, dict):
        return None
    table = str(ref.get("table") or "").strip()
    record_id = str(ref.get("record_id") or ref.get("id") or "").strip()
    if not record_id:
        return None
    return (table, record_id)


def ref_record_ids(refs: Any) -> List[str]:
    """Just the record ids from a ref list, both key shapes."""
    out: List[str] = []
    for ref in refs if isinstance(refs, list) else []:
        parsed = ref_record_key(ref)
        if parsed and parsed[1]:
            out.append(parsed[1])
    return out


def _delete_entity_cascade(conn: sqlite3.Connection, entity_id: str) -> Dict[str, int]:
    """Hard-delete one entity and its mention/edge/vector/review footprint.

    Closes the open dossier (valid_to) rather than deleting the signal_object
    row — same provenance-preserving choice as orphan prune.

    Refuses outright for a black-holed entity. The check lives HERE as well as in
    the selectors that call it: a keep-clause is something the next caller can
    forget, and this module has three reap paths that each grew their own copy of
    the keep rules. The selectors filter so their reported counts stay honest;
    this is the guarantee.

    It deliberately resolves the protected set ITSELF rather than accepting the
    hoisted one the selectors compute. Taking the caller's snapshot made the
    backstop useless in exactly the case it exists for — a protection applied
    between the candidate scan and this delete was invisible, because the
    snapshot predated it. The lookup is one indexed read of a table that holds a
    handful of rows, against a loop that deletes tens of entities; correctness
    wins that trade easily. Skips and logs rather than raising —
    this is an internal maintenance path, and failing an unrelated scrub gives
    the caller nothing it can act on. ``M4``'s invariant test is what turns a
    skip into a red build.
    """
    normalized_name: Optional[str] = None
    try:
        row = conn.execute(
            "SELECT normalized_name FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if row:
            normalized_name = str(row[0] or "") or None
    except sqlite3.Error:
        normalized_name = None
    if _is_protected(conn, entity_id, normalized_name):
        logger.warning(
            "scrub: refused to delete black-holed entity %s (%s) — "
            "a selector let a protected entity reach the cascade",
            entity_id,
            normalized_name or "?",
        )
        return {
            "mentions": 0,
            "edges": 0,
            "vectors": 0,
            "reviews": 0,
            "dossiers_closed": 0,
            "skipped_protected": 1,
        }

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

    So does anything still carrying an edge. Those hubs are recognised by type
    and id prefix, but ``fact_materializer`` and ``graph_enrichers`` mint their
    vertices through ``EntityResolver.resolve``, which hands out ordinary
    ``ent_`` ids and ordinary types — invisible to both tests above. They are
    mention-less by nature (a vertex is not a sighting), so every scrub reaped
    them and ``_delete_entity_cascade`` took their edges too, and the next
    derivation run built the same nodes and edges again. The graph a scrub left
    behind was a subset of the graph until derivation caught up: 567 nodes
    where the rebuilt graph holds 1,625. An edge is what makes a vertex load-
    bearing, so having one is reason enough to keep it; once the edge goes, the
    next pass reaps the node.
    """
    # Synthetic / enrichment-only nodes — never mention-backed.
    keep_clause = (
        "AND entity_type NOT IN ('goal', 'conversation', 'claim', 'program', 'document') "
        "AND entity_id NOT LIKE 'goal_%' "
        "AND entity_id NOT LIKE 'topic_%' "
        "AND entity_id NOT LIKE 'conv_%' "
        "AND entity_id NOT LIKE 'claim:%' "
        "AND entity_id NOT LIKE 'program:%' "
        "AND entity_id NOT LIKE 'transcript:%' "
        "AND NOT EXISTS (SELECT 1 FROM entity_edges g"
        "                WHERE g.src_entity_id = e.entity_id"
        "                   OR g.dst_entity_id = e.entity_id)"
    )
    try:
        rows = conn.execute(
            f"""
            SELECT entity_id, normalized_name FROM entities e
            WHERE mention_count = 0 AND is_self = 0
              AND (contact_id IS NULL
                   OR NOT EXISTS (SELECT 1 FROM contacts c WHERE c.contact_id = e.contact_id))
              {keep_clause}
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # No contacts table in this database — fall back to the anchor-blind rule.
        # Same alias, so the shared keep_clause binds here too.
        rows = conn.execute(
            f"""
            SELECT entity_id, normalized_name FROM entities e
            WHERE mention_count = 0 AND contact_id IS NULL AND is_self = 0
              {keep_clause}
            """
        ).fetchall()
    # Black-holed entities are filtered out of the candidate list, not just
    # refused by the cascade, so the returned ids (which callers report as
    # "entities_removed") describe what actually went.
    protected = _protected_entity_keys(conn)
    orphan_ids: List[str] = []
    retained_protected: List[str] = []
    for row in rows:
        entity_id, normalized = str(row[0]), str(row[1] or "") or None
        if _is_protected(conn, entity_id, normalized, protected=protected):
            retained_protected.append(entity_id)
            continue
        orphan_ids.append(entity_id)
    for entity_id in orphan_ids:
        _delete_entity_cascade(conn, entity_id)
    # Callers report len(orphan_ids) as "entities_removed"; a retention is a
    # different outcome and reporting it as neither leaves the owner unable to
    # tell "nothing was orphaned" from "something was protected".
    _delete_orphan_entities.last_retained_protected = list(retained_protected)  # type: ignore[attr-defined]
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
            f"SELECT entity_id, canonical_name, normalized_name FROM entities WHERE {keep_clause}"
        ).fetchall()
    except sqlite3.OperationalError:
        return report

    # A black-holed entity is never junk, however its surface reads. The C4
    # predicate rejects truncated and punctuation-heavy names — exactly the
    # shape of a compound place name like "Old Harbor- Rey's Place" — so
    # without this filter the one-shot scrub is a second route to the reap the
    # cascade guard exists to stop.
    protected = _protected_entity_keys(conn)
    junk: List[tuple] = []
    for entity_id, canonical_name, normalized_name in rows:
        if _is_protected(
            conn, str(entity_id), str(normalized_name or "") or None, protected=protected
        ):
            continue
        if not is_valid_entity_surface(str(canonical_name or "")):
            junk.append((str(entity_id), str(canonical_name or "")))

    report["junk_entities_found"] = len(junk)
    report["samples"] = [
        {"entity_id": eid, "canonical_name": name} for eid, name in junk[:20]
    ]
    if dry_run or not junk:
        return report

    with batched_writes(conn):
        for entity_id, _name in junk:
            counts = _delete_entity_cascade(conn, entity_id)
            if counts.get("skipped_protected"):
                # A protection applied between the scan and this loop. Reporting it
                # as removed would be a false receipt for a row that is still there.
                report["junk_entities_retained_protected"] = (
                    report.get("junk_entities_retained_protected", 0) + 1
                )
                continue
            report["junk_entities_removed"] += 1
            report["mentions_removed"] += counts["mentions"]
            report["edges_removed"] += counts["edges"]

    from ..entities.maintenance import rebuild_evidence_edges

    # rebuild_evidence_edges manages the gate itself (M2.2) — wrapping it here
    # would reinstate a whole-rebuild exclusive hold.
    rebuilt = rebuild_evidence_edges(conn)
    report["edges_rebuilt"] = rebuilt
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
    """Recompute evidence edges after a scrub.

    Decayed weights cannot subtract removed evidence, so co_occurrence is
    rebuilt from surviving mentions and communicates_with from thread
    co-participation (P3.2). part_of edges are structural (derived from
    names, not scrubbed evidence) and are kept for surviving entities.

    Delegates to the shared entity-graph rebuild so both edge types are
    recreated — previously this deleted communicates_with but only rebuilt
    co_occurrence, silently dropping every co-participation edge on each scrub.
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

    with with_db_write():
        conn.execute("DELETE FROM stat_state")
        conn.execute("DELETE FROM stat_seen")
        commit_connection(conn)

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
    stale = [fact_id for (fact_id,) in rows if str(fact_id) not in live_ids]
    if stale:
        with batched_writes(conn):
            for fact_id in stale:
                conn.execute("DELETE FROM signal_facts WHERE fact_id=?", (fact_id,))
    return {"insights_written": written, "insights_pruned": len(stale)}


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
            ref_key = ref_record_key(ref)
            if ref_key and ref_key[1]:
                all_ref_ids.add(ref_key[1])

    source_by_record = _record_source_map(conn, all_ref_ids)
    scrubbed_records = scrubbed_record_ids or set()

    def _ref_is_scrubbed(ref: Dict[str, Any]) -> bool:
        ref_key = ref_record_key(ref)
        record_id = ref_key[1] if ref_key else ""
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
    # The per-fact judgement is in-memory (maps precomputed above), so the
    # batch hold covers writes only.
    with batched_writes(conn):
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
    return {"facts_deleted": deleted, "facts_trimmed": trimmed}


_REF_ID_COLUMNS = ("record_id", "message_id", "entry_id", "event_id", "transaction_id", "id")


#: Spine id prefixes. An id shaped like this names an ENTITY, not a canonical
#: record, whatever table the ref claims.
_SPINE_ID_PREFIXES = ("ent_", "goal_", "topic_", "conv_")


def _ref_record_exists(conn: sqlite3.Connection, table: str, record_id: str) -> Optional[bool]:
    """Does the referenced record still exist? None = unverifiable (be conservative)."""
    # A spine id resolves against `entities`, whatever table the ref names.
    #
    # `entities/dossier.py` writes its provenance as
    # ``{"table": "entity_mentions", "record_id": "ent_01f5a601f0a84816"}`` — an
    # ENTITY id in a record_id field, pointed at a table keyed on `mention_id`.
    # It can never match, so a liveness check that trusts the declared table
    # reports GONE for a dossier whose entity is perfectly alive.
    #
    # Measured on the owner's node 2026-08-27 while dry-running a widened sweep:
    # 164 refs carry this shape, and **158 of them name an entity that still
    # exists**. Trusting the table would have closed 158 live dossiers — the
    # widening's first act would have been data loss.
    #
    # Resolving by shape rather than by the declared table is not a workaround
    # for a malformed ref so much as reading what it means: the id identifies the
    # thing unambiguously, and the table label is the part that is wrong.
    if record_id.startswith(_SPINE_ID_PREFIXES):
        try:
            row = conn.execute(
                "SELECT 1 FROM entities WHERE entity_id=? LIMIT 1", (record_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return row is not None
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
    """Close any active derived object whose every source_ref points at a deleted record.

    Widened from ``object_type='fact'`` 2026-08-27. The restriction was never
    principled — a dossier, a PlaceContext and a fact all outlive their evidence
    the same way — and combined with reading only the ``record_id`` key it left
    the sweep looking at 7% of the provenance in the database.

    Dry-run against the owner's node before widening, and it earned the caution:
    the first pass would have closed 164 of 170 dossiers, 158 of them citing
    entities that are perfectly alive, because ``entities/dossier.py`` writes an
    ENTITY id into a ``record_id`` field aimed at ``entity_mentions``. That is
    fixed in ``_ref_record_exists`` by resolving a spine-shaped id against
    ``entities`` rather than the mis-declared table.

    After that correction the sweep closes 1,497 objects, and every one was
    checked against all nine canonical tables and the entity spine: zero are
    reachable. The bulk are derived from ``chatgpt_ingestion`` records the owner
    deleted, whose derived rows outlived them — the leak this sweep exists for,
    which had simply never been allowed to look outside ``fact``.

    Original note follows.

    Close active facts whose every source_ref points at a deleted record.

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
        WHERE valid_to IS NULL
        """
    ).fetchall()
    # Read pass first: the per-ref liveness checks are SELECTs, so they must
    # not run under the gate. Writes happen in one short gated pass below.
    to_close: List[str] = []
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
            ref_key = ref_record_key(ref)
            table = ref_key[0] if ref_key else ""
            record_id = ref_key[1] if ref_key else ""
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
            to_close.append(str(object_id))
    if not to_close:
        return 0
    with batched_writes(conn):
        for object_id in to_close:
            conn.execute(
                "UPDATE signal_objects SET valid_to=datetime('now'), updated_at=datetime('now') "
                "WHERE object_id=?",
                (object_id,),
            )
    return len(to_close)


# ----------------------------------------------------------------- orphans


def sweep_orphans(conn: sqlite3.Connection) -> Dict[str, int]:
    out: Dict[str, int] = {}
    with with_db_write():
        cursor = conn.execute(
            """
            DELETE FROM embedding_entities WHERE embedding_id NOT IN (
                SELECT embedding_id FROM signal_embeddings
            )
            """
        )
        out["embedding_entities"] = int(cursor.rowcount or 0)
        # A conflict row is an orphan when the fact it CHALLENGES is gone. A3
        # quarantine rows have no incumbent by design — an unroutable or withheld
        # assertion is not challenging anything — so they carry the synthetic
        # sentinel `quarantine:<reason>` (writer.py::_quarantine). That sentinel is
        # never a signal_objects.object_id, so the orphan predicate matched every
        # one of them: measured on the live node 2026-08-26, all 13 pending rows
        # would be deleted by this sweep, and it fires on the owner's ordinary
        # per-record exclusion flow. That queue is the human review path for
        # third-party assertions — including health.condition about people who
        # never consented — so deleting it silently discards exactly the decisions
        # a person is supposed to make.
        cursor = conn.execute(
            """
            DELETE FROM fact_conflicts
             WHERE incumbent_object_id NOT LIKE 'quarantine:%'
               AND incumbent_object_id NOT IN (
                SELECT object_id FROM signal_objects WHERE object_type='fact'
            )
            """
        )
        out["fact_conflicts"] = int(cursor.rowcount or 0)
        cursor = conn.execute(
            "DELETE FROM entity_review WHERE candidate_entity_id NOT IN (SELECT entity_id FROM entities)"
        )
        out["entity_review"] = int(cursor.rowcount or 0)
        # The derived-object index is keyed by object_id, so it survives every
        # sweep above by construction: nothing here carries a source_id it could
        # be matched on. Its rows are rendered SENTENCES naming people, and the
        # producer that would prune them next runs on the next enrichment batch
        # — which is not a schedule a deletion request gets to be put on.
        from ..signal.derived_index import prune_orphaned_derived_embeddings

        out["derived_object_index"] = prune_orphaned_derived_embeddings(conn)
        commit_connection(conn)
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

    with batched_writes(conn):
        report["entities_recounted"] = _recount_entity_mentions(conn)
        orphans = _delete_orphan_entities(conn)
    report["entities_removed"] = len(orphans)
    report["entities_retained_protected"] = len(
        getattr(_delete_orphan_entities, "last_retained_protected", []) or []
    )
    # Self-gating (M2.2) — must not run under a caller's hold.
    report["edges_rebuilt"] = _rebuild_entity_edges(conn)

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
    commit_connection(conn)
    return report


def _purge_extraction_artifacts_for_records(
    conn: sqlite3.Connection, id_set: Set[str]
) -> Dict[str, int]:
    """Drop extraction artifacts whose only evidence is these records.

    ``extraction_artifacts`` carries ``source_refs_json`` in exactly the shape
    ``ref_record_key`` reads, and **no lifecycle sweep opened the table at all**.
    Measured on the owner's node 2026-08-27: 5,817 rows, 5,814 of them with a
    resolvable ref — 3,273 to conversation_messages, 2,497 to journal_entries.
    Every one survived a record deletion that named its source.

    Trimmed rather than deleted when other records still evidence the artifact,
    matching how ``signal_objects`` is treated a few lines above: a derived row
    with surviving evidence is still true, just less so.
    """
    out = {"extraction_artifacts_deleted": 0, "extraction_artifacts_trimmed": 0}
    if not id_set or not _table_exists(conn, "extraction_artifacts"):
        return out
    rows = conn.execute(
        "SELECT artifact_id, source_refs_json FROM extraction_artifacts"
    ).fetchall()
    with batched_writes(conn):
        for artifact_id, refs_json in rows:
            try:
                refs = json.loads(refs_json or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(refs, list) or not refs:
                continue
            surviving = []
            for ref in refs:
                key = ref_record_key(ref)
                if key is None or key[1] not in id_set:
                    surviving.append(ref)
            if len(surviving) == len(refs):
                continue
            if surviving:
                conn.execute(
                    "UPDATE extraction_artifacts SET source_refs_json=? WHERE artifact_id=?",
                    (json.dumps(surviving), str(artifact_id)),
                )
                out["extraction_artifacts_trimmed"] += 1
            else:
                conn.execute(
                    "DELETE FROM extraction_artifacts WHERE artifact_id=?", (str(artifact_id),)
                )
                out["extraction_artifacts_deleted"] += 1
    return out


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

    from ...storage.adapters.sqlite.stores import SQLiteVectorIndex

    embedding_ids = [
        str(r[0])
        for r in conn.execute(
            f"SELECT embedding_id FROM signal_embeddings WHERE record_id IN ({placeholders})",
            ids,
        ).fetchall()
    ]
    with batched_writes(conn):
        conn.execute(f"DELETE FROM signal_embeddings WHERE record_id IN ({placeholders})", ids)
        if embedding_ids:
            SQLiteVectorIndex(conn).delete_embeddings(embedding_ids)

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
        report["entities_retained_protected"] = len(
            getattr(_delete_orphan_entities, "last_retained_protected", []) or []
        )
    report["embeddings_removed"] = len(embedding_ids)
    # Self-gating (M2.2) — must not run under a caller's hold.
    report["edges_rebuilt"] = _rebuild_entity_edges(conn)

    # Aggregates: same non-subtractability as source scrub -> refold.
    report.update(refold_statistics(conn))
    report.update(repromote_stat_insights(conn))

    # Derived objects evidenced only by these records are removed.
    #
    # Two restrictions used to make this a near no-op, and they compounded.
    # It read `record_id` off each ref while 92% of live refs key on `id`, and
    # it looked only at `object_type='fact'`. Measured on the owner's node
    # 2026-08-27: the pair saw **307 of 3,245 refs** and **127 of 3,262 active
    # objects** — so "remove this from my intelligence" reached about 4% of the
    # derived layer it names. A PlaceContext or a topic summary derived from a
    # withdrawn record outlives it exactly as a fact would.
    id_set = set(ids)
    rows = conn.execute(
        "SELECT object_id, source_refs_json FROM signal_objects"
    ).fetchall()
    facts_deleted = 0
    facts_trimmed = 0
    with batched_writes(conn):
        for object_id, refs_json in rows:
            try:
                refs = json.loads(refs_json or "[]")
            except json.JSONDecodeError:
                refs = []
            if not refs:
                continue
            surviving = []
            for r in refs:
                key = ref_record_key(r)
                # A ref this reader cannot resolve (a day-scoped aggregate) is
                # not evidence for or against these records — keep it, so an
                # unverifiable ref never causes a deletion.
                if key is None or key[1] not in id_set:
                    surviving.append(r)
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
    report.update(_purge_extraction_artifacts_for_records(conn, id_set))

    try:
        from ..entities.dossier import refresh_dossiers

        report["dossiers_refreshed"] = refresh_dossiers(conn)
    except Exception as exc:  # noqa: BLE001
        report["dossiers_refreshed"] = f"failed: {exc}"
    report["orphans"] = sweep_orphans(conn)
    commit_connection(conn)
    return report
