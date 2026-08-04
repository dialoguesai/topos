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

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .blackhole import BlackholeStore, normalize_entity_name

logger = logging.getLogger("topos.features.lifecycle.blackhole_rebuild")

# Prose-bearing signal_objects: these hold names as text with no id to filter on.
PROSE_OBJECT_TYPES = (
    "entity_dossier",
    "attention_summary",
    "interest_profile",
    "top_topics",
)


@dataclass
class RebuildReport:
    entity_ref: str
    objects_closed: int = 0
    briefs_invalidated: int = 0
    stat_insights_removed: int = 0
    context_vectors_removed: int = 0
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entity_ref": self.entity_ref,
            "objects_closed": self.objects_closed,
            "briefs_invalidated": self.briefs_invalidated,
            "stat_insights_removed": self.stat_insights_removed,
            "context_vectors_removed": self.context_vectors_removed,
            **self.details,
        }


def _terms_for(store: BlackholeStore, entity_ref: str) -> Set[str]:
    record = store.get(entity_ref)
    if record is None:
        return set()
    terms = {record["normalized_name"], *record.get("aliases", [])}
    return {t for t in terms if t}


def _mentions(text: Optional[str], terms: Set[str]) -> bool:
    if not text:
        return False
    haystack = normalize_entity_name(str(text))
    return any(term in haystack for term in terms)


def _close_prose_objects(conn: sqlite3.Connection, terms: Set[str], entity_id: str) -> int:
    """Close any signal_object whose payload names the entity, or that *is* it."""
    closed = 0
    rows = conn.execute(
        f"""
        SELECT object_id, object_key, payload_json FROM signal_objects
        WHERE object_type IN ({','.join('?' for _ in PROSE_OBJECT_TYPES)})
          AND valid_to IS NULL
        """,
        PROSE_OBJECT_TYPES,
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
        report.objects_closed = _close_prose_objects(conn, terms, record["entity_id"])
        report.briefs_invalidated = _invalidate_briefs(conn, terms)
        report.stat_insights_removed = _remove_stat_insights(conn, terms)
        report.context_vectors_removed = _remove_context_vector(
            conn, str(record.get("entity_id") or "")
        )
        conn.commit()
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
        "%s insights removed, %s context vectors removed",
        report.objects_closed,
        report.briefs_invalidated,
        report.stat_insights_removed,
        report.context_vectors_removed,
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
