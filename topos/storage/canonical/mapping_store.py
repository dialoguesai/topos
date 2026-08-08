"""Source record → canonical id mapping store."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from ..db.write_gate import batched_writes, commit_connection, with_db_write


@dataclass(frozen=True)
class MappingRecord:
    source_id: str
    source_record_id: str
    canonical_id: str
    canonical_table: str = "ai_chat_messages"


class MappingStore:
    def get_mapping(self, source_id: str, source_record_id: str) -> Optional[MappingRecord]:
        raise NotImplementedError

    def save_mapping(self, record: MappingRecord) -> None:
        raise NotImplementedError


class SQLiteMappingStore(MappingStore):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        from ..db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)

    def get_mapping(self, source_id: str, source_record_id: str) -> Optional[MappingRecord]:
        row = self._conn.execute(
            """
            SELECT canonical_id, canonical_table FROM canonical_source_mappings
            WHERE source_id=? AND source_record_id=?
            """,
            (source_id, source_record_id),
        ).fetchone()
        if row is None:
            return None
        return MappingRecord(
            source_id=source_id,
            source_record_id=source_record_id,
            canonical_id=row[0],
            canonical_table=row[1],
        )

    def save_mapping(self, record: MappingRecord) -> None:
        # commit_connection no-ops inside save_mappings_batch's batched_writes;
        # the batch commits once at exit.
        with with_db_write():
            self._conn.execute(
                """
                INSERT INTO canonical_source_mappings (source_id, source_record_id, canonical_table, canonical_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, source_record_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id,
                    canonical_table=excluded.canonical_table
                """,
                (record.source_id, record.source_record_id, record.canonical_table, record.canonical_id),
            )
            commit_connection(self._conn)

    def save_mappings_batch(self, records: Iterable[MappingRecord]) -> None:
        batch = list(records)
        if not batch:
            return
        with batched_writes(self._conn):
            for record in batch:
                self.save_mapping(record)


class InMemoryMappingStore(MappingStore):
    def __init__(self) -> None:
        self._records: Dict[str, MappingRecord] = {}

    def get_mapping(self, source_id: str, source_record_id: str) -> Optional[MappingRecord]:
        return self._records.get(f"{source_id}:{source_record_id}")

    def save_mapping(self, record: MappingRecord) -> None:
        self._records[f"{record.source_id}:{record.source_record_id}"] = record

    def save_mappings_batch(self, records: Iterable[MappingRecord]) -> None:
        for record in records:
            self.save_mapping(record)
