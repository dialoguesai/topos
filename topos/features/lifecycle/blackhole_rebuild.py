"""M4: rebuild the derived layers so a black hole actually takes effect.

Filtering at read time handles anything that carries an entity id. It cannot
handle the artifacts that carry the *name as prose* — a dimension brief, an
attention digest's mover label, a dossier summary, a stat's group key. Those
were written before the entity was protected, and no read-time predicate can
un-write them.

So flipping the flag starts a rebuild, and D4 says the owner is told *first*:
the notification is raised at flag time (`BlackholeStore.blackhole_entity`) and
resolves only when this job finishes. In between, `BlackholeGuard` withholds the
whole class of prose artifacts from non-owners rather than serving a stale one —
fail closed, never stale.

D3 is full exclusion, so the rule this job applies is *withdraw*, not *redact*:
an artifact that mentions a protected entity is invalidated outright rather than
patched to remove the name. A summary with a name-shaped hole in it still says
someone was there, and a half-scrubbed aggregate silently keeps the protected
entity's contribution in its numbers.

Invalidated artifacts are closed the way the rest of the system closes things
(`valid_to`), not deleted: the owner's own history stays intact, and the normal
producers regenerate clean versions on their next run.
"""

from __future__ import annotations

import html
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ...storage.db.write_gate import batched_writes
from .blackhole import BlackholeStore, normalize_entity_name

logger = logging.getLogger("topos.features.lifecycle.blackhole_rebuild")

# Prose-bearing signal_objects: these hold names as text with no id to filter on.
#
# Retained for callers and tests that name it, but withdrawal is no longer scoped
# to this tuple — see WITHDRAW_ALL_DERIVED_OBJECTS below.
PROSE_OBJECT_TYPES = (
    "entity_dossier",
    "attention_summary",
    "interest_profile",
    "top_topics",
)

#: Object types that name the entity with NO spine id a predicate can reach.
#:
#: Added after measuring the live node 2026-08-27. Both carry a protected place
#: name in ``display_band`` and were outside every withdrawal:
#:
#:   PlaceContext        76 rows, object_key ``309-coffee-georgetown``
#:   AvailabilityWindow  406 rows
#:
#: Compare a fact's key, ``fact:ent_2f6bb2c5e3164b42:…`` — that carries a spine
#: entity id, so ``BlackholeGuard.sql_exclusion`` pushes a predicate onto it and
#: ``test_rebuild_leaves_id_joinable_facts_intact`` deliberately keeps it: the
#: claim is owner truth that is already safely hidden. These two are keyed by a
#: SLUG. There is no ``ent_`` anywhere in the object, so only the name text
#: identifies the protected entity — and a read-time name scan is precisely the
#: fallback this rebuild exists to make unnecessary.
#:
#: Two attempts to replace this list with a mechanical rule both failed, and the
#: existing suite caught both. "Withdraw everything derived" deletes the facts.
#: "Leave anything carrying the entity id" keeps the dossiers. The line between a
#: structured claim worth keeping and a generated restatement worth withdrawing is
#: a judgment, so it stays written down — but it is a list, and a list rots, which
#: is what ``test_no_live_object_type_names_a_protected_entity_unreachably``
#: exists to prevent.
UNREACHABLE_OBJECT_TYPES = (
    "PlaceContext",
    "AvailabilityWindow",
)

#: Everything the rebuild withdraws from.
WITHDRAWN_OBJECT_TYPES = PROSE_OBJECT_TYPES + UNREACHABLE_OBJECT_TYPES


@dataclass
class RebuildReport:
    entity_ref: str
    objects_closed: int = 0
    briefs_invalidated: int = 0
    stat_insights_removed: int = 0
    context_vectors_removed: int = 0
    cluster_labels_withdrawn: int = 0
    embeddings_withdrawn: int = 0
    goals_withdrawn: int = 0
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entity_ref": self.entity_ref,
            "objects_closed": self.objects_closed,
            "briefs_invalidated": self.briefs_invalidated,
            "stat_insights_removed": self.stat_insights_removed,
            "context_vectors_removed": self.context_vectors_removed,
            "cluster_labels_withdrawn": self.cluster_labels_withdrawn,
            "embeddings_withdrawn": self.embeddings_withdrawn,
            "goals_withdrawn": self.goals_withdrawn,
            **self.details,
        }


def _terms_for(store: BlackholeStore, entity_ref: str) -> Set[str]:
    record = store.get(entity_ref)
    if record is None:
        return set()
    terms = {record["normalized_name"], *record.get("aliases", [])}
    return {t for t in terms if t}


def _decoded_variants(text: str) -> List[str]:
    """The forms a protected name can be hiding in inside stored text.

    Withdrawal greps stored strings, and those strings are not always the plain
    text a reader sees. Two live misses on the owner's node 2026-08-27, both of
    which left the protected place name in place while the rebuild reported
    ``complete``:

    * ``signal_objects.payload_json`` holds JSON, and ``json.dumps`` escapes a
      curly apostrophe to ``\u2019``. The raw column therefore contains
      ``Old Harbor- Jeff\u2019s Place`` — normalizing that yields
      ``... jeff u2019s place``, which matches nothing. Four derived objects
      (PlaceContext, AvailabilityWindow, attention_summary) named the place and
      were invisible to the sweep.
    * an embedding's ``search_text`` carried the name HTML-escaped
      (``Jeff&#39;s``), from content that had passed through an HTML lane.

    Returning several decodings and testing all of them is cheaper and more
    honest than trying to canonicalise every producer.
    """
    variants = [text]
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        decoded = None
    if decoded is not None:
        variants.append(json.dumps(decoded, ensure_ascii=False))
    if "&#" in text or "&amp;" in text:
        variants.append(html.unescape(text))
    return variants


def _mentions(text: Optional[str], terms: Set[str]) -> bool:
    if not text:
        return False
    for variant in _decoded_variants(str(text)):
        haystack = normalize_entity_name(variant)
        if any(term in haystack for term in terms):
            return True
    return False


def _close_prose_objects(conn: sqlite3.Connection, terms: Set[str], entity_id: str) -> int:
    """Close any DERIVED object whose payload names the entity, or that *is* it.

    Scope widened from an object-type allowlist to every derived object: see
    WITHDRAW_ALL_DERIVED_OBJECTS. The name is kept for its callers.
    """
    closed = 0
    rows = conn.execute(
        f"""
        SELECT object_id, object_key, payload_json FROM signal_objects
        WHERE object_type IN ({','.join('?' for _ in WITHDRAWN_OBJECT_TYPES)})
          AND valid_to IS NULL
        """,
        WITHDRAWN_OBJECT_TYPES,
    ).fetchall()
    for object_id, object_key, payload_json in rows:
        # The entity's own dossier is keyed by id; everything else is matched on
        # the text, since that is the only handle these artifacts give us.
        hit = bool(entity_id) and str(object_key or "").endswith(f":{entity_id}")
        if not hit:
            hit = _mentions(payload_json, terms)
        if not hit:
            continue
        conn.execute(
            "UPDATE signal_objects SET valid_to=datetime('now'), updated_at=datetime('now') "
            "WHERE object_id=?",
            (object_id,),
        )
        closed += 1
    return closed


def _invalidate_briefs(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Blank a brief's body when it names the entity.

    The row is kept so the brief's identity and revision history survive; the
    producer rewrites the body on its next run. An empty body reads as "not
    generated yet", which is exactly the state it is now in.
    """
    try:
        rows = conn.execute(
            "SELECT brief_id, markdown_body FROM signal_dimension_briefs"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    invalidated = 0
    for brief_id, body in rows:
        if not _mentions(body, terms):
            continue
        conn.execute(
            "UPDATE signal_dimension_briefs SET markdown_body='' WHERE brief_id=?",
            (brief_id,),
        )
        invalidated += 1
    return invalidated


def _remove_stat_insights(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Drop promoted insights grouped by the protected entity.

    The underlying stat state keeps folding — it is owner-only, and the owner may
    lift the black hole later — but the packaged insight goes, and with it the
    entity's contribution to anything derived from it (D3).
    """
    try:
        rows = conn.execute("SELECT fact_id, payload_json FROM signal_facts").fetchall()
    except sqlite3.OperationalError:
        return 0
    removed = 0
    for fact_id, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not _mentions(payload.get("group_key") or payload_json, terms):
            continue
        conn.execute("DELETE FROM signal_facts WHERE fact_id=?", (fact_id,))
        removed += 1
    return removed


def _withdraw_embeddings(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Remove retrieval documents that name the entity — vectors, FTS and all.

    The rest of this module deliberately leaves canonical rows alone: they are the
    owner's own record, and read-time filtering is what protects them. The
    embedding lane is not canonical. It is a DERIVED index, it is what a
    similarity search actually reaches, and it was not covered — a black hole
    marked ``complete`` on the owner's node 2026-08-27 left the protected place
    name live in ``text_preview``, ``search_text`` and the FTS index, so the name
    was still full-text searchable after the withdrawal reported success.

    These rows are deleted rather than blanked. A blanked embedding is a
    zero-information vector still occupying a point in the ANN index — the
    ``[ADDRESS]`` pathology, where 126 identical placeholder vectors sit
    equidistant from every address-shaped query. And unlike a canonical row this
    is cheap to lose: if the owner lifts the black hole, re-enrichment rebuilds it.

    Ordering matters. The vec companion rows are dropped through
    ``SQLiteVectorIndex`` FIRST, because it needs the embedding ids; the base
    DELETE then fires ``signal_embeddings_ad``, the external-content FTS5 delete
    trigger, which is the only thing that takes the term out of the full-text
    index. A raw DELETE without the first step leaves the ANN shadow behind.
    """
    try:
        rows = conn.execute(
            "SELECT embedding_id, text_preview, search_text FROM signal_embeddings"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    doomed = [
        str(embedding_id)
        for embedding_id, preview, search_text in rows
        if _mentions(preview, terms) or _mentions(search_text, terms)
    ]
    if not doomed:
        return 0

    from ...storage.adapters.sqlite.stores import SQLiteVectorIndex

    try:
        SQLiteVectorIndex(conn).delete_embeddings(doomed)
    except Exception as exc:  # noqa: BLE001
        # Never leave the base row behind on an ANN failure: a searchable
        # document is a worse outcome than an orphaned vector companion.
        logger.warning("blackhole: ANN companion delete failed (%s); continuing", exc)
    placeholders = ",".join("?" for _ in doomed)
    conn.execute(
        f"DELETE FROM signal_embeddings WHERE embedding_id IN ({placeholders})", doomed
    )
    return len(doomed)


def _withdraw_goals(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Drop extracted goals whose text names the entity.

    ``user_goals`` is LLM-extracted prose like the artifacts above, but it is not
    in ``signal_objects`` and so was reached by none of them — and it has its own
    unfiltered path into an answer (``retrieval._load_user_goals`` reads the table
    directly, bypassing the embedding corpus entirely). Removed rather than
    closed, matching ``_remove_stat_insights``: the table has no validity window
    to close.
    """
    try:
        rows = conn.execute("SELECT goal_id, goal_text, payload_json FROM user_goals").fetchall()
    except sqlite3.OperationalError:
        return 0
    removed = 0
    for goal_id, goal_text, payload_json in rows:
        if not (_mentions(goal_text, terms) or _mentions(payload_json, terms)):
            continue
        conn.execute("DELETE FROM user_goals WHERE goal_id=?", (goal_id,))
        removed += 1
    return removed


def _remove_context_vector(conn: sqlite3.Connection, entity_id: str) -> int:
    """Drop the mention-context centroid for a protected entity.

    Affinity edges that already name the entity stay (id-joinable; owner keeps
    them, ``BlackholeGuard`` hides them from everyone else). The centroid is
    the producer input for *new* latent edges, so it must not survive the
    blackhole rebuild — the next nightly affinity pass freezes any remaining
    edges and will not invent fresh ones from a missing centroid.
    """
    if not entity_id:
        return 0
    try:
        return int(
            conn.execute(
                "DELETE FROM entity_context_vectors WHERE entity_id=?",
                (entity_id,),
            ).rowcount
            or 0
        )
    except sqlite3.OperationalError:
        return 0


#: Cluster metadata lists that carry entity NAMES rather than ids. A read-time
#: guard filters by ``entity_id`` and cannot see these, which is why the label
#: needs a producer-side refusal — and why these need withdrawing here, for the
#: rows minted before the entity was protected.
_NAME_BEARING_METADATA_KEYS = ("related_entities", "query_aliases")


def _strip_metadata_names(metadata: dict, terms: Set[str]) -> bool:
    """Drop protected names from a cluster's metadata lists, in place.

    Returns True when anything was removed. ``query_aliases`` is derived from
    ``related_entities``, so a name left in either one is still served.
    """
    if not isinstance(metadata, dict):
        return False
    changed = False
    for key in _NAME_BEARING_METADATA_KEYS:
        values = metadata.get(key)
        if not isinstance(values, list) or not values:
            continue
        kept = [v for v in values if not _mentions(str(v), terms)]
        if len(kept) != len(values):
            metadata[key] = kept
            changed = True
    return changed


def _withdraw_cluster_labels(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Take the protected name out of topic cluster labels and previews.

    Closing the derived ``top_topics`` object is not enough on its own: the
    object is minted FROM ``topic_clusters.label`` by
    ``write_top_topics_signal_facts``, so the next cluster pass would write the
    withdrawn name straight back. The row is the producer, and it has to be
    cleaned or the withdrawal lasts until the next recompute.

    The cluster itself is kept — it is real structure in the owner's own data,
    and deleting it would take the members with it. What goes is the prose: the
    label falls back to the deterministic term label when that is clean, and to
    the neutral placeholder when it is not (term labels are built from member
    text, so they can carry the name too). The centroid preview is a verbatim
    member excerpt, so it is blanked rather than rewritten.
    """
    try:
        rows = conn.execute(
            "SELECT cluster_id, label, centroid_preview, metadata_json FROM topic_clusters"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    cleaned = 0
    for cluster_id, label, centroid_preview, metadata_json in rows:
        label_hit = _mentions(label, terms)
        preview_hit = _mentions(centroid_preview, terms)
        try:
            metadata = json.loads(metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {}
        metadata_hit = _strip_metadata_names(metadata, terms)
        if not label_hit and not preview_hit and not metadata_hit:
            continue
        new_label = str(label or "")
        if label_hit:
            term_label = str((metadata or {}).get("term_label") or "")
            new_label = term_label if term_label and not _mentions(term_label, terms) else "topic cluster"
        conn.execute(
            "UPDATE topic_clusters SET label=?, centroid_preview=?, metadata_json=?,"
            " updated_at=datetime('now') WHERE cluster_id=?",
            (
                new_label,
                "" if preview_hit else centroid_preview,
                json.dumps(metadata) if metadata_hit else metadata_json,
                cluster_id,
            ),
        )
        cleaned += 1
    return cleaned


def _withdraw_cluster_member_previews(conn: sqlite3.Connection, terms: Set[str]) -> int:
    """Blank member excerpts that quote the protected entity.

    A member row is a verbatim slice of a canonical record, served by
    `signal_list_topic_cluster_members` and carried in the retrieval packet. The
    row itself stays — it is the cluster's own membership, and the id is what
    the read-side guard joins on — but the quoted text goes.
    """
    try:
        rows = conn.execute(
            "SELECT member_id, text_preview FROM topic_cluster_members"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    blanked = 0
    for member_id, text_preview in rows:
        if not _mentions(text_preview, terms):
            continue
        conn.execute(
            "UPDATE topic_cluster_members SET text_preview='' WHERE member_id=?",
            (member_id,),
        )
        blanked += 1
    return blanked


def rebuild_for_blackhole(conn: sqlite3.Connection, entity_ref: str) -> RebuildReport:
    """Withdraw every prose artifact naming this entity, then mark the flag ready.

    Idempotent: a second run finds nothing left to close and simply confirms the
    completed state.
    """
    store = BlackholeStore(conn)
    record = store.get(entity_ref)
    report = RebuildReport(entity_ref=entity_ref)
    if record is None:
        report.details["status"] = "not_blackholed"
        return report

    terms = _terms_for(store, entity_ref)
    if not terms:
        report.details["status"] = "no_terms"
        return report

    store.mark_rebuild_running(entity_ref)
    try:
        with batched_writes(conn):
            report.objects_closed = _close_prose_objects(conn, terms, record["entity_id"])
            report.briefs_invalidated = _invalidate_briefs(conn, terms)
            report.stat_insights_removed = _remove_stat_insights(conn, terms)
            report.context_vectors_removed = _remove_context_vector(
                conn, str(record.get("entity_id") or "")
            )
            report.cluster_labels_withdrawn = _withdraw_cluster_labels(conn, terms)
            report.details["cluster_member_previews_blanked"] = (
                _withdraw_cluster_member_previews(conn, terms)
            )
            report.embeddings_withdrawn = _withdraw_embeddings(conn, terms)
            report.goals_withdrawn = _withdraw_goals(conn, terms)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        # Leaves rebuild_state='failed', which keeps the withholding in force.
        store.mark_rebuild_failed(entity_ref, reason=str(exc))
        report.details["status"] = "failed"
        report.details["error"] = str(exc)
        logger.warning("blackhole rebuild failed for %s: %s", entity_ref, exc)
        return report

    store.mark_rebuild_complete(entity_ref)
    report.details["status"] = "complete"
    logger.info(
        "blackhole rebuild complete: %s objects closed, %s briefs blanked, "
        "%s insights removed, %s context vectors removed, %s cluster labels withdrawn, "
        "%s embeddings withdrawn, %s goals withdrawn",
        report.objects_closed,
        report.briefs_invalidated,
        report.stat_insights_removed,
        report.context_vectors_removed,
        report.cluster_labels_withdrawn,
        report.embeddings_withdrawn,
        report.goals_withdrawn,
    )
    return report


def run_pending_rebuilds(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Process every black hole still awaiting a rebuild.

    Safe to call on node start and after any blackhole write — a rebuild that
    was interrupted (or failed) is retried, and the fail-closed withholding
    stays in force until one succeeds.
    """
    store = BlackholeStore(conn)
    pending = [r for r in store.list() if r["rebuild_state"] != "complete"]
    return [rebuild_for_blackhole(conn, r["normalized_name"]).as_dict() for r in pending]


def rerun_all_rebuilds(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Re-run the rebuild for every black hole, completed ones included.

    A rebuild is only ever as complete as the surface list it knew about. When
    this job learns a new one — topic cluster labels and member excerpts did not
    exist in its list until now — entities already marked ``complete`` are
    exactly the ones carrying the un-withdrawn data, and ``run_pending_rebuilds``
    skips precisely those. One live node had five member excerpts still quoting
    a protected entity under three ``complete`` rebuilds.
    """
    store = BlackholeStore(conn)
    return [rebuild_for_blackhole(conn, r["normalized_name"]).as_dict() for r in store.list()]
