"""Record-level "this job ran" markers, shared by both enrichment lanes.

``enrichment_record_progress`` witnesses the RUN; the coverage tables witness the
OUTPUT. The distinction is the whole point: a record that ran and legitimately
produced nothing writes no coverage row, so a coverage-only "already done" check
re-scans it forever. Measured on the live node 2026-08-25, imessage/entities: a
backfill of 2,400 records reported 1,288 processed, and 1,903 of the same window
still counted as missing afterwards, because three in five messages ("ok",
"haha", an emoji) contain no named entity.

This module exists because only ONE of the two lanes was writing markers. The
manual `/enrichment` backfill did; the automatic ingest lane — the one that runs
on every sync — did not. Measured 2026-08-27, the table held **0 rows on a node
with 38,838 derived facts**, so nothing was ever skippable and every re-sync
re-derived and appended. That is what multiplies derived rows 2x-4.3x across
re-syncs, and it is why a fabricated row is never one row to retract but N.

Kept in `enrichment/` rather than `api/` so the orchestrator can use it without a
layering inversion, and so the two lanes cannot drift into two definitions of
"processed" — the failure this whole workstream keeps finding.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.enrichment.record_progress")

_RECORD_ID_KEYS = (
    "message_id",
    "record_id",
    "event_id",
    "entry_id",
    "transaction_id",
    "contact_id",
)


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return False
    return row is not None


def _record_identifier(record: Dict[str, Any]) -> Optional[str]:
    for key in _RECORD_ID_KEYS:
        value = record.get(key)
        if value:
            return str(value)
    return None


def _processed_record_ids(conn, source_id: str, job_id: str, min_spec: int) -> set[str]:
    """Ids this job has already run over, at or above ``min_spec``.

    Distinct from coverage: coverage witnesses OUTPUT, this witnesses the RUN.
    Returns empty on any failure — the caller then falls back to coverage-only
    behaviour, which over-scans rather than skipping work that never happened.
    """
    if not conn:
        return set()
    try:
        if not _table_exists(conn, "enrichment_record_progress"):
            return set()
        rows = conn.execute(
            "SELECT record_id FROM enrichment_record_progress "
            "WHERE source_id=? AND job_id=? AND COALESCE(spec_version, 0) >= ?",
            (source_id, job_id, int(min_spec or 0)),
        ).fetchall()
        return {str(row[0]) for row in rows if row and row[0]}
    except Exception as exc:  # noqa: BLE001 — a marker read must never fail a backfill
        logger.warning("enrichment_record_progress read failed for %s/%s: %s", source_id, job_id, exc)
        return set()


def _mark_records_processed(
    conn, source_id: str, job_id: str, records: List[Dict[str, Any]], spec_version: int
) -> int:
    """Record that this job ran over these records, whatever it produced.

    Best-effort by design: the enrichment already happened and its output is
    committed, so a failure here costs a future re-scan, never correctness.
    """
    if not conn or not records:
        return 0
    ids = [rid for rid in (_record_identifier(r) for r in records) if rid]
    if not ids:
        return 0
    try:
        if not _table_exists(conn, "enrichment_record_progress"):
            return 0
        from ..storage.db.write_gate import batched_writes

        # `batched_writes` holds the write gate across the statements AND the
        # commit. Doing the executemany bare and only calling commit_connection
        # takes SQLite's write lock ungated, which the gate detects and warns
        # about — it can deadlock-until-busy_timeout against whoever holds the
        # gate, and this runs right after a long enrichment batch, when that is
        # most likely.
        with batched_writes(conn):
            conn.executemany(
                "INSERT INTO enrichment_record_progress "
                "(source_id, job_id, record_id, spec_version) VALUES (?,?,?,?) "
                "ON CONFLICT(source_id, job_id, record_id) DO UPDATE SET "
                "spec_version=excluded.spec_version, processed_at=datetime('now')",
                [(source_id, job_id, rid, int(spec_version or 0)) for rid in ids],
            )
        return len(ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("enrichment_record_progress write failed for %s/%s: %s", source_id, job_id, exc)
        return 0


# Public names. The underscored ones are kept because `api/enrichment.py` has
# used them since v1.3.25 and its call sites read clearly as-is.
record_identifier = _record_identifier
processed_record_ids = _processed_record_ids
mark_records_processed = _mark_records_processed
