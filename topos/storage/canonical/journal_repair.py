"""One-time repair for journal rows dated to the importer's clock.

Grow's journal producers wrote ``entry_at`` from the import clock while the true
session time sat in ``starts_at`` — on the first live node checked, 309 rows
across two sources, 127 of them sharing ``2026-08-08T03:34:44`` to the second.
``SQLiteCanonicalStore._journal_entry_at`` closes this for new writes; rows
already on disk need this sweep.

Why it matters downstream: the entity graph dates its edges from canonical event
time, so a batch of months-old sessions all looked like they happened at the
import instant and pulled years-old relationships into the recent graph window.
The timeline projection was never affected — it already reads ``starts_at`` —
so this brings ``entry_at`` into agreement with what timeline always showed.

The predicate is the corruption signature itself: a row that states its own time
as the exact second it was ingested, while separately knowing when its session
started, is reporting the importer's clock. A genuine ``entry_at`` is never
touched, so the sweep is safe to re-run.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict

logger = logging.getLogger("topos.storage.canonical.journal_repair")

# entry_at == ingested_at to the second, and a real session start is available.
_SIGNATURE = """
    starts_at IS NOT NULL AND TRIM(starts_at) <> ''
    AND entry_at IS NOT NULL
    AND ingested_at IS NOT NULL
    AND substr(entry_at, 1, 19) = substr(ingested_at, 1, 19)
"""


def repair_ingest_clock_dates(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> Dict[str, Any]:
    """Re-date journal rows that recorded the import clock as their event time."""
    try:
        by_source = {
            str(source_id): int(n)
            for source_id, n in conn.execute(
                f"SELECT source_id, COUNT(*) FROM journal_entries WHERE {_SIGNATURE} "
                "GROUP BY source_id"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        logger.warning("journal date repair could not read journal_entries: %s", exc)
        return {"status": "skipped", "error": str(exc), "repaired": 0}

    total = sum(by_source.values())
    if not total or dry_run:
        return {
            "status": "dry_run" if dry_run else "ok",
            "repaired": 0,
            "candidates": total,
            "by_source": by_source,
        }

    from ..db.write_gate import commit_connection, with_db_write

    with with_db_write():
        conn.execute(
            f"UPDATE journal_entries SET entry_at = starts_at WHERE {_SIGNATURE}"
        )
        commit_connection(conn)

    logger.info(
        "journal date repair: re-dated %d row(s) from starts_at %s", total, by_source
    )
    return {"status": "ok", "repaired": total, "by_source": by_source}
