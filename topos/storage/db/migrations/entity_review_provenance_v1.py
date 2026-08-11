"""Retire review questions the owner never raised, and undo a wrong bar.

``entity_review_dedup_v1`` collapsed the queue to one row per decision; this
retires the decisions that should never have been asked at all. The resolver
queued a review for every near-match, including the ones raised by derivation:
``fact_materializer`` and ``graph_enrichers`` resolve a bare string to get a
graph node for an edge endpoint, so the "surface" is a cluster tag, not
something the owner wrote. Those entities are mention-less, ``derived_scrub``
deletes them, the next run mints them again — one surface reached 478 pending
rows that way. ``EntityResolver._queue_review`` now gates on ``record_id``, so
no new ones arrive.

Profiled on the first live node, pending resolution rows split cleanly:

    no record_id (derivation) | 2,886 rows |  18 decisions
    has record_id (ingest)    |    88 rows |  76 decisions

Rows are marked ``stale`` rather than deleted: nothing is lost, the queue and
its count skip them (both filter on ``status``), and a surface the owner
actually uses will raise the question again with provenance attached.

This also repairs ``entity_review_quality_bar_v1``, a short-lived version of
this migration that gated on ``mention_count`` instead. That bar was wrong —
557 of the 558 mention-less person entities on the live node are seeded
contacts, and "is 'Delta Whiskey' your contact 'Alex'?" is the most valuable
question this queue asks. Where it reached one node it staled 15 rows that
carry a record_id; those are restored below.

``no_bind`` rows are owner guards, not questions, and are left untouched; so
are ``merge`` rows, which the sweep already holds to its own bar.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_review_provenance_v1"

# The id of the withdrawn mention-count version. It never shipped, so a node
# carrying it has one orphan bookkeeping row and 15 wrongly-retired questions.
_WITHDRAWN_ID = "entity_review_quality_bar_v1"


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_entity_review_provenance_v1_up(conn: sqlite3.Connection) -> None:
    if "kind" in _columns(conn, "entity_review"):
        # Repair first, so a row the withdrawn bar retired is judged again on
        # provenance below rather than left retired for the wrong reason. Only
        # on a node that actually ran it — otherwise this would reopen whatever
        # a later migration has legitimately retired. Safe to key on status
        # alone: the only other writer of 'stale' (resolve_review /
        # merge_entity_pair) stales rows that reference an entity it is about to
        # delete, so those never survive the candidate join — and resolution
        # rows carry no subject_entity_id to match on.
        ran_withdrawn = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id = ?", (_WITHDRAWN_ID,)
        ).fetchone()
        if ran_withdrawn:
            conn.execute(
                """
                UPDATE entity_review SET status = 'pending'
                WHERE status = 'stale' AND kind = 'resolution'
                  AND record_id IS NOT NULL AND trim(record_id) != ''
                  AND candidate_entity_id IN (SELECT entity_id FROM entities)
                """
            )
        conn.execute(
            """
            UPDATE entity_review SET status = 'stale'
            WHERE status = 'pending' AND kind = 'resolution'
              AND (record_id IS NULL OR trim(record_id) = '')
            """
        )
    conn.execute(
        "DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (_WITHDRAWN_ID,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_review_provenance_v1_down(conn: sqlite3.Connection) -> None:
    # Deliberately does not un-stale: 'stale' is also what an approved merge
    # leaves behind, so reopening by status would resurrect unanswerable rows.
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
