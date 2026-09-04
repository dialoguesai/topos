"""Synchronous, idempotent projection of canonical rows into the timeline."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from ..storage.db.write_gate import batched_writes, commit_connection
from .lifecycle.exclusions import excluded_record_ids
from .stats.definitions import row_event_ts
from .stats.engine import _record_id, _table_for_row
from .stats.fold import parse_ts

CANONICAL_TIMELINE_TABLES = (
    "ai_chat_messages",
    "conversation_messages",
    "journal_entries",
    "calendar_events",
    "profile_records",
    "activity_events",
    "financial_transactions",
    "location_events",
    "transcript_segments",
)
CANONICAL_RECORD_ID_COLUMNS = {
    "ai_chat_messages": "message_id",
    "conversation_messages": "message_id",
    "journal_entries": "entry_id",
    "calendar_events": "event_id",
    "profile_records": "record_id",
    "activity_events": "event_id",
    "financial_transactions": "transaction_id",
    "location_events": "event_id",
    "transcript_segments": "segment_id",
}


@dataclass
class TimelineProjectionResult:
    candidates: int = 0
    written: int = 0
    existing: int = 0
    timestamp_mismatch: int = 0
    rendering_normalized: int = 0
    identity_mismatch: int = 0
    excluded: int = 0
    missing_timestamp: int = 0
    missing_record_id: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

    def add(self, other: "TimelineProjectionResult") -> None:
        for field_name in self.__dataclass_fields__:
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))


def normalize_timeline_renderings(
    conn: sqlite3.Connection, *, dry_run: bool = True
) -> Dict[str, int]:
    """Collapse timeline rows that are one instant stored under two renderings.

    The projection can no longer create these, but it also cannot reach the ones
    already written: its identity lookup takes a single row and asks whether the
    INSTANT changed, and for a twin the answer is no. So the backlog needs an
    explicit pass.

    Rows are grouped by ``(record_id, source_id, canonical_table)`` and then by
    PARSED INSTANT — never by record alone. One ``time_log`` record on the
    owner's node carries 10 genuinely distinct events under a shared
    ``record_id``; grouping by record would have destroyed 9 real events while
    "removing duplicates". A duplicate here means the same moment written twice,
    nothing looser.

    The surviving row keeps the canonical ``isoformat()`` rendering and the
    richest metadata across the group — a later pass may have attached entities
    or a dimension to whichever twin it happened to find, and that enrichment
    belongs to the record, not to the spelling of its timestamp.
    """
    groups: Dict[tuple, list] = {}
    for row in conn.execute(
        """
        SELECT event_at, record_id, source_id, canonical_table, record_type,
               entity_ids_json, signal_dimension, cluster_id, created_at
        FROM timeline
        """
    ).fetchall():
        parsed = parse_ts(str(row[0]))
        if parsed is None:
            continue
        groups.setdefault((row[1], row[2], row[3], parsed), []).append(row)

    stats = {"groups": 0, "rows_removed": 0, "metadata_merged": 0, "rewritten": 0}
    plan = []
    for (record_id, source_id, table, parsed), rows in groups.items():
        canonical = parsed.isoformat()
        if len(rows) == 1 and str(rows[0][0]) == canonical:
            continue
        stats["groups"] += 1
        stats["rows_removed"] += len(rows) - 1
        # Richest wins per field, independent of which rendering carried it.
        best_type = next((r[4] for r in rows if r[4]), None)
        best_entities = next((r[5] for r in rows if r[5] and r[5] != "[]"), "[]")
        best_dimension = next((r[6] for r in rows if r[6]), None)
        best_cluster = next((r[7] for r in rows if r[7]), None)
        keeper = next((r for r in rows if str(r[0]) == canonical), rows[0])
        if (best_type, best_entities, best_dimension, best_cluster) != (
            keeper[4], keeper[5] or "[]", keeper[6], keeper[7]
        ):
            stats["metadata_merged"] += 1
        if str(keeper[0]) != canonical:
            stats["rewritten"] += 1
        plan.append(
            (record_id, source_id, table, [str(r[0]) for r in rows], canonical,
             best_type, best_entities, best_dimension, best_cluster, keeper[8])
        )

    if dry_run or not plan:
        return stats

    with batched_writes(conn):
        for (record_id, source_id, table, renderings, canonical, rtype,
             entities, dimension, cluster, created_at) in plan:
            for rendering in renderings:
                conn.execute(
                    "DELETE FROM timeline WHERE event_at=? AND record_id=?",
                    (rendering, record_id),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO timeline (
                    event_at, record_id, source_id, canonical_table, record_type,
                    entity_ids_json, cluster_id, signal_dimension, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (canonical, record_id, source_id, table, rtype,
                 entities or "[]", cluster, dimension, created_at),
            )
    return stats


def project_timeline_rows(
    conn: sqlite3.Connection,
    rows: Iterable[Dict[str, Any]],
    *,
    missing_only: bool = False,
    commit: bool = True,
    exclusions: Optional[set[str]] = None,
    dry_run: bool = False,
) -> TimelineProjectionResult:
    """Project canonical rows into ``timeline`` without destructive rewrites.

    ``missing_only`` is intended for repair jobs. Normal live projection updates
    mutable metadata on an existing event while preserving non-empty metadata
    already stored by a richer signal-derivation pass.
    """

    result = TimelineProjectionResult()
    excluded = excluded_record_ids(conn) if exclusions is None else exclusions

    insert_missing_sql = """
        INSERT OR IGNORE INTO timeline (
            event_at, record_id, source_id, canonical_table,
            record_type, entity_ids_json, signal_dimension
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    upsert_sql = """
        INSERT INTO timeline (
            event_at, record_id, source_id, canonical_table,
            record_type, entity_ids_json, signal_dimension
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_at, record_id) DO UPDATE SET
            source_id=COALESCE(excluded.source_id, timeline.source_id),
            canonical_table=COALESCE(excluded.canonical_table, timeline.canonical_table),
            record_type=COALESCE(excluded.record_type, timeline.record_type),
            entity_ids_json=CASE
                WHEN excluded.entity_ids_json <> '[]' THEN excluded.entity_ids_json
                ELSE timeline.entity_ids_json
            END,
            signal_dimension=COALESCE(excluded.signal_dimension, timeline.signal_dimension)
    """

    # dry_run performs no writes and needs no gate. Live runs hold the gate for
    # the batch and commit at exit: per-batch commits replace the caller's
    # single end commit, because an open write transaction must never span a
    # gate release (write_gate lock-order inversion).
    with nullcontext() if dry_run else batched_writes(conn):
        for row in rows:
            result.candidates += 1
            record_id = _record_id(row)
            if not record_id:
                result.missing_record_id += 1
                continue
            if record_id in excluded:
                result.excluded += 1
                continue
            event_at = row_event_ts(row)
            if event_at is None:
                result.missing_timestamp += 1
                continue

            values = (
                event_at.isoformat(),
                record_id,
                row.get("source_id"),
                _table_for_row(row) or None,
                row.get("record_type"),
                json.dumps(row.get("entity_ids") or []),
                row.get("signal_dimension"),
            )
            identity_row = conn.execute(
                """
                SELECT event_at, record_type, entity_ids_json, signal_dimension
                FROM timeline
                WHERE record_id=? AND canonical_table IS ? AND source_id IS ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (record_id, values[3], values[2]),
            ).fetchone()
            if identity_row is not None:
                existing_event_at = str(identity_row[0])
                existing_ts = parse_ts(existing_event_at)
                # Two different questions, and conflating them silently doubled
                # rows. `parse_ts` answers "is this the same INSTANT", but the
                # primary key is the raw STRING. `2026-06-28T18:00:00` and
                # `2026-06-28T18:00:00+00:00` parse equal and key apart, so the
                # instant check reported "nothing to do" while the insert landed
                # a second row — and every future projection agreed there was
                # nothing to do, because the check that would delete the twin is
                # the one declaring it fine. 195 such shadows on the owner's
                # node, all written in one backfill window on 2026-07-10, none
                # reachable by re-projection.
                if existing_ts is not None:
                    timestamp_changed = existing_ts != event_at
                    rendering_changed = not timestamp_changed and existing_event_at != values[0]
                else:
                    timestamp_changed = existing_event_at != values[0]
                    rendering_changed = False
                if timestamp_changed or rendering_changed:
                    if timestamp_changed:
                        result.timestamp_mismatch += 1
                    else:
                        result.rendering_normalized += 1
                    if dry_run or missing_only:
                        result.existing += 1
                        continue
                    preserved_record_type = values[4] or identity_row[1]
                    preserved_entities = (
                        values[5] if values[5] != "[]" else str(identity_row[2] or "[]")
                    )
                    preserved_dimension = values[6] or identity_row[3]
                    values = (*values[:4], preserved_record_type, preserved_entities, preserved_dimension)
                    conn.execute(
                        """
                        DELETE FROM timeline
                        WHERE record_id=? AND canonical_table IS ? AND source_id IS ?
                        """,
                        (record_id, values[3], values[2]),
                    )
                elif dry_run or missing_only:
                    result.existing += 1
                    continue
            else:
                conflicting_row = conn.execute(
                    """
                    SELECT source_id, canonical_table
                    FROM timeline
                    WHERE event_at=? AND record_id=?
                    LIMIT 1
                    """,
                    values[:2],
                ).fetchone()
                if conflicting_row is not None:
                    result.identity_mismatch += 1
                    if dry_run:
                        result.written += 1
                        continue
                    conn.execute(
                        """
                        UPDATE timeline
                        SET source_id=?, canonical_table=?,
                            record_type=COALESCE(?, record_type)
                        WHERE event_at=? AND record_id=?
                        """,
                        (values[2], values[3], values[4], values[0], values[1]),
                    )
                    result.written += 1
                    continue
                if dry_run:
                    result.written += 1
                    continue
            before = conn.total_changes
            conn.execute(insert_missing_sql if missing_only else upsert_sql, values)
            if conn.total_changes > before:
                result.written += 1
            else:
                result.existing += 1

    return result


def project_canonical_timeline(
    conn: sqlite3.Connection,
    *,
    source_id: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    missing_only: bool = True,
    dry_run: bool = False,
    commit: bool = True,
    batch_size: int = 500,
) -> Dict[str, Any]:
    """Project eligible rows from every canonical table and return a repair report."""

    total = TimelineProjectionResult()
    by_table: Dict[str, Dict[str, int]] = {}
    exclusions = excluded_record_ids(conn)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for table in CANONICAL_TIMELINE_TABLES:
            try:
                query = f"SELECT * FROM {table}"  # table is from a fixed allowlist
                params: tuple[Any, ...] = ()
                if source_id:
                    query += " WHERE source_id=?"
                    params = (source_id,)
                cursor = conn.execute(query, params)
            except sqlite3.OperationalError:
                continue

            table_result = TimelineProjectionResult()
            while True:
                fetched = cursor.fetchmany(max(1, int(batch_size)))
                if not fetched:
                    break
                rows = []
                for raw_row in fetched:
                    row = dict(raw_row)
                    row["_table"] = table
                    event_at = row_event_ts(row)
                    if date_from and (event_at is None or event_at < date_from):
                        continue
                    if date_to and (event_at is None or event_at > date_to):
                        continue
                    rows.append(row)
                batch_result = project_timeline_rows(
                    conn,
                    rows,
                    missing_only=missing_only,
                    commit=False,
                    exclusions=exclusions,
                    dry_run=dry_run,
                )
                table_result.add(batch_result)
            total.add(table_result)
            by_table[table] = table_result.to_dict()
    finally:
        conn.row_factory = previous_factory

    if commit and not dry_run:
        # Each batch already committed inside project_timeline_rows; this only
        # catches a stray implicit transaction.
        commit_connection(conn)
    orphaned_total = 0
    orphaned_by_table_source: Dict[str, int] = {}
    orphaned_samples = []
    for table, id_column in CANONICAL_RECORD_ID_COLUMNS.items():
        try:
            query = f"""
                SELECT t.record_id, t.source_id
                FROM timeline t
                WHERE t.canonical_table=?
                  AND NOT EXISTS (
                      SELECT 1 FROM {table} c
                      WHERE c.{id_column}=t.record_id AND c.source_id IS t.source_id
                  )
            """
            params: list[Any] = [table]
            if source_id:
                query += " AND t.source_id=?"
                params.append(source_id)
            if date_from:
                query += " AND t.event_at>=?"
                params.append(date_from.isoformat())
            if date_to:
                query += " AND t.event_at<=?"
                params.append(date_to.isoformat())
            rows = conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            continue
        orphaned_total += len(rows)
        for record_id, orphan_source_id in rows:
            key = f"{table}:{orphan_source_id or 'unknown'}"
            orphaned_by_table_source[key] = orphaned_by_table_source.get(key, 0) + 1
            if len(orphaned_samples) < 20:
                orphaned_samples.append(
                    {
                        "record_id": str(record_id),
                        "source_id": orphan_source_id,
                        "canonical_table": table,
                    }
                )

    return {
        "totals": total.to_dict(),
        "by_table": by_table,
        "orphaned": {
            "total": orphaned_total,
            "by_table_source": orphaned_by_table_source,
            "samples": orphaned_samples,
        },
    }


def timeline_coverage_for_source(conn: sqlite3.Connection, source_id: str) -> Dict[str, Any]:
    """Return timeline parity counts for coverage and only_missing backfill."""
    report = project_canonical_timeline(
        conn,
        source_id=source_id,
        dry_run=True,
        missing_only=True,
        commit=False,
    )
    totals = report["totals"]
    invalid = (
        int(totals.get("excluded", 0))
        + int(totals.get("missing_timestamp", 0))
        + int(totals.get("missing_record_id", 0))
    )
    total = max(0, int(totals.get("candidates", 0)) - invalid)
    missing = int(totals.get("written", 0)) + int(totals.get("identity_mismatch", 0))
    enriched = max(0, total - missing)
    coverage_percent = round(min(100.0, enriched / total * 100.0), 1) if total else 0.0
    return {
        "total_records": total,
        "enriched_records": enriched,
        "missing_records": missing,
        "coverage_percent": coverage_percent,
        "report": report,
    }


def repair_timeline_for_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    missing_only: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Repair missing timeline rows for one source using the shared projector."""
    return project_canonical_timeline(
        conn,
        source_id=source_id,
        missing_only=missing_only,
        dry_run=dry_run,
        commit=not dry_run,
    )
