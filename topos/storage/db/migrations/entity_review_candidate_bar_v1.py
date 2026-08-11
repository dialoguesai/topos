"""Retire review questions whose candidate has never been seen in the data.

``entity_review_provenance_v1`` retired the questions the owner never raised;
this retires the ones with nothing on the other side. The resolver proposed a
merge into any same-type entity that scored in the review band, with no regard
for whether that entity had ever appeared in ingested content — so a first name
found in a message was offered against whichever address-book entry it happened
to resemble.

Contacts get no exemption. An address book is not a list of important people:
most imported contacts are someone met in passing, often years ago, who will
never come up again. On the first live node, 35 of the 39 open contact
questions proposed a never-mentioned contact — "Alex Karp" (the Palantir CEO)
as the owner's contact "Alex", "Alexis" as "Alex", "Alice" as "Allie". Of 76
open decisions there, 24 clear this bar.

``EntityResolver._queue_review`` now applies ``MIN_MENTIONS_FOR_MERGE`` up
front, so no new ones arrive. Rows here are marked ``stale`` rather than
deleted: nothing is lost, the queue and its count skip them (both filter on
``status``), and when the candidate is finally named in ingested content it
earns mentions and the question comes back with evidence behind it.

``no_bind`` rows are owner guards, not questions, and are left untouched; so
are ``merge`` rows, which the sweep already holds to this same bar.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_review_candidate_bar_v1"

# Frozen copy of resolver.MIN_MENTIONS_FOR_MERGE: a migration must keep making
# the judgement it made the day it was written, even if the app's bar moves.
_MIN_MENTIONS = 2


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_entity_review_candidate_bar_v1_up(conn: sqlite3.Connection) -> None:
    if "kind" in _columns(conn, "entity_review"):
        conn.execute(
            """
            UPDATE entity_review SET status = 'stale'
            WHERE status = 'pending' AND kind = 'resolution'
              AND candidate_entity_id IN (
                  SELECT entity_id FROM entities
                  WHERE COALESCE(mention_count, 0) < ?
              )
            """,
            (_MIN_MENTIONS,),
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_review_candidate_bar_v1_down(conn: sqlite3.Connection) -> None:
    # Deliberately does not un-stale: 'stale' is also what an approved merge
    # leaves behind, so reopening by status would resurrect unanswerable rows.
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
