"""Record-level "this ran" markers, so a backfill stops re-scanning empty results.

`only_missing` builds its done-set from ids PRESENT in a job's coverage table.
That is a proxy for "was processed" and it is wrong in one direction: a record
that ran and legitimately produced nothing writes no coverage row, so it looks
unprocessed forever and is re-scanned by every subsequent backfill.

Measured on the live node 2026-08-25, imessage/entities: a backfill of 2,400
records reported 1,288 processed, and afterwards 1,903 of the same 2,355-message
window still counted as "missing" — because roughly three in five messages
("ok", "haha", an emoji) contain no named entity and NER correctly emits nothing
for them. Every future backfill would pay for those again, indefinitely.

This table records the fact of processing independently of its output: one row
per (source, job, record) with the catalog `spec_version` it ran under. A job
whose spec_version bumps invalidates its markers by the same `>=` predicate the
coverage tables already use (see enrichment_spec_version_v1), so a genuine
re-derivation still reprocesses everything.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "enrichment_record_progress_v1"


def apply_enrichment_record_progress_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS enrichment_record_progress (
            source_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            spec_version INTEGER NOT NULL DEFAULT 0,
            processed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (source_id, job_id, record_id)
        )
        """
    )
    # The only read this table serves: "which records of this source has this
    # job already run over, at or above this spec". Covering index so the
    # done-set lookup stays a single index scan as the corpus grows.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_enrichment_record_progress_lookup
        ON enrichment_record_progress (source_id, job_id, spec_version)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
