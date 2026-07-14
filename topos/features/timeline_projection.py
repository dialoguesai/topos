"""Synchronous, idempotent projection of canonical rows into the timeline."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

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
}


@dataclass
class TimelineProjectionResult:
    candidates: int = 0
    written: int = 0
    existing: int = 0
    timestamp_mismatch: int = 0
    identity_mismatch: int = 0
    excluded: int = 0
    missing_timestamp: int = 0
    missing_record_id: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

    def add(self, other: "TimelineProjectionResult") -> None:
        for field_name in self.__dataclass_fields__:
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))


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
            timestamp_changed = (
                existing_ts != event_at if existing_ts is not None else existing_event_at != values[0]
            )
            if timestamp_changed:
                result.timestamp_mismatch += 1
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

    if commit and not dry_run:
        conn.commit()
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
        conn.commit()
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
