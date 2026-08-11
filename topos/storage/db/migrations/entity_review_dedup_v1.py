"""Collapse the entity-review queue to one row per owner decision.

The resolver queued a review row per *mention*, so the People-tab queue filled
with the same handful of questions asked hundreds of times over — burying the
real merge candidates below them and re-asking questions the owner had already
answered. ``resolver._queue_review`` now asks once; this clears the backlog it
left behind:

  * rows pointing at an entity that no longer exists — unanswerable, and the
    queue already hid them, so they only inflated the count;
  * pending merge rows whose subject is gone — approving one would fail;
  * pending rows for a decision the owner already settled;
  * pending duplicates of each other, keeping the sighting that asked first.

``no_bind`` rows are owner guards, not questions, and are left untouched.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_review_dedup_v1"

# Frozen copy of consolidation._decision_key_sql: a migration must keep
# collapsing rows the way it did the day it was written, even if the app's
# notion of a decision later changes.
_DECISION_KEY = (
    "kind || '|' || COALESCE(subject_entity_id,"
    " 'surface:' || lower(trim(surface_text))) || '|' || candidate_entity_id"
)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_entity_review_dedup_v1_up(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "entity_review")
    if {"kind", "subject_entity_id"} <= cols:
        conn.execute(
            """
            DELETE FROM entity_review
            WHERE kind IN ('merge', 'resolution')
              AND candidate_entity_id NOT IN (SELECT entity_id FROM entities)
            """
        )
        conn.execute(
            """
            DELETE FROM entity_review
            WHERE status = 'pending' AND kind = 'merge'
              AND (subject_entity_id IS NULL
                   OR subject_entity_id NOT IN (SELECT entity_id FROM entities))
            """
        )
        conn.execute(
            f"""
            DELETE FROM entity_review
            WHERE status = 'pending'
              AND ({_DECISION_KEY}) IN (
                  SELECT {_DECISION_KEY} FROM entity_review
                  WHERE status IN ('approved', 'dismissed')
              )
            """
        )
        conn.execute(
            f"""
            DELETE FROM entity_review
            WHERE status = 'pending' AND review_id NOT IN (
                SELECT review_id FROM (
                    SELECT review_id, ROW_NUMBER() OVER (
                        PARTITION BY {_DECISION_KEY}
                        ORDER BY created_at ASC, review_id ASC
                    ) AS rn
                    FROM entity_review WHERE status = 'pending'
                ) WHERE rn = 1
            )
            """
        )
        # The resolver checks "have I already asked this?" on every mention it
        # cannot place, so that lookup sits in the ingest path.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_review_candidate"
            " ON entity_review(kind, candidate_entity_id)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_review_dedup_v1_down(conn: sqlite3.Connection) -> None:
    # Deleted duplicates are derived rows; the resolver re-queues any decision
    # that is still open the next time it meets the surface.
    conn.execute("DROP INDEX IF EXISTS idx_entity_review_candidate")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
