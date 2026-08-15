"""Retire url_classification: drop its table and the interest tags it wrote.

The job classified every visited page into DMOZ top-level categories and wrote
them to ``browser_url_classification``. On the node this was measured on, six
months of operation produced 8,616 rows of which 73% were the single label
"Reference" — at an average confidence of 0.961, HIGHER than any other bucket,
so no threshold could separate signal from filler. The same host drew different
labels on different visits (github.com: Reference 720, Computers 125, Kids 5;
google.com: Porn 12), because the classifier reads the page TITLE, and the
title of an authenticated app page carries almost nothing.

It is a taxonomy mismatch rather than a tuning problem: DMOZ categories describe
where a site files in a web directory, not what a person is doing, and
"Reference" is that scheme's catch-all.

The rows reached the interests dimension through
``scope_materializer._materialize_activity_tags``, which upserted each category
as an ``activity_tags`` signal object — 458 of the dimension's 828 objects, 349
of them "Reference". Deleting the source table without those leaves interests
asserting a dominant tag that nothing can regenerate or explain.

Scoped by ``payload_json.source_kind`` so the keyword-rule tags that function
also produces are untouched, and so nothing else in the dimension is caught:
the personal ``fact`` objects (goes_by, hails_from) live in interests too and
have nothing to do with this.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "retire_url_classification_v1"

_SOURCE_KIND = "browser_url_classification"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def apply_retire_url_classification_v1_up(conn: sqlite3.Connection) -> None:
    """Drop the classification table and the interest tags derived from it."""
    removed_tags = 0
    if _table_exists(conn, "signal_objects"):
        cur = conn.execute(
            """
            DELETE FROM signal_objects
            WHERE signal_dimension='interests'
              AND object_type='activity_tags'
              AND json_extract(payload_json, '$.source_kind') = ?
            """,
            (_SOURCE_KIND,),
        )
        removed_tags = int(cur.rowcount or 0)

    had_table = _table_exists(conn, "browser_url_classification")
    if had_table:
        conn.execute("DROP TABLE IF EXISTS browser_url_classification")

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    # Unconditional, and not inside the branches above: migrations commit
    # internally (the runner does not), and a DELETE that matched zero rows has
    # still opened an implicit transaction. Leaving it open holds the write lock
    # for the rest of the process — every later migration in the chain then dies
    # on "database is locked", which is exactly how this was caught.
    conn.commit()

    if removed_tags or had_table:
        logger.info(
            "[MIGRATION:%s] retired url_classification — dropped table=%s, "
            "removed %d interest activity_tags",
            MIGRATION_ID,
            had_table,
            removed_tags,
        )
