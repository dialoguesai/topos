"""Owner-only record selections; access restrictions, never deletion tombstones.

Legacy derived stores do not have complete lineage. While any record is protected,
their non-owner aggregate/summary releases must be withheld unless a reader can
filter its *inputs* exactly. A source reference list alone is not a completeness
certificate. Do not make this floor swappable with an NL membership evaluator.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional, Set

from ...storage.db.write_gate import commit_connection, with_db_write


class RecordProtectionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list(self) -> List[Dict[str, Any]]:
        try:
            rows = self.conn.execute(
                "SELECT canonical_table, record_id, note, created_at, updated_at "
                "FROM owner_only_records ORDER BY canonical_table, record_id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: owner_only_records" in str(exc).lower():
                try:
                    migrated = self.conn.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id='owner_only_records_v1'").fetchone()
                except sqlite3.OperationalError as ledger_exc:
                    if "no such table: wiki_schema_migrations" not in str(ledger_exc).lower():
                        raise
                    migrated = None
                if migrated:
                    raise sqlite3.OperationalError("owner-only protection schema is unavailable") from exc
                return []
            raise
        return [dict(zip(("canonical_table", "record_id", "note", "created_at", "updated_at"), r)) for r in rows]

    def blocked_ids(self, canonical_table: Optional[str] = None) -> Set[str]:
        return {r["record_id"] for r in self.list() if canonical_table is None or r["canonical_table"] == canonical_table}

    def supported_tables(self) -> List[str]:
        """Advertise only concrete canonical stores with selectable native IDs."""
        from ...storage.adapters.sqlite.stores import _NATIVE_ID_COL

        supported = []
        for table, id_column in _NATIVE_ID_COL.items():
            if self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is None:
                continue
            columns = {row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")')}
            if id_column in columns:
                supported.append(table)
        return sorted(supported)

    def protect(self, *, canonical_table: str, record_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        from ...storage.adapters.sqlite.stores import _NATIVE_ID_COL

        table = str(canonical_table or "").strip()
        rid = str(record_id or "").strip()
        if table not in _NATIVE_ID_COL or not rid or len(rid) > 500:
            raise ValueError("A supported canonical table and record id are required")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise ValueError("note must be at most 500 characters")
        # Identifiers come only from the closed storage adapter registry.
        try:
            present = self.conn.execute(
                f'SELECT 1 FROM "{table}" WHERE "{_NATIVE_ID_COL[table]}"=? LIMIT 1', (rid,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                raise ValueError("Canonical record not found") from exc
            raise
        if not present:
            raise ValueError("Canonical record not found")
        with with_db_write():
            self.conn.execute(
                "INSERT INTO owner_only_records (canonical_table, record_id, note) VALUES (?, ?, ?) "
                "ON CONFLICT(canonical_table, record_id) DO UPDATE SET note=excluded.note, updated_at=datetime('now')",
                (table, rid, note),
            )
            commit_connection(self.conn)
        return next(r for r in self.list() if r["canonical_table"] == table and r["record_id"] == rid)

    def unprotect(self, *, canonical_table: str, record_id: str) -> bool:
        with with_db_write():
            cursor = self.conn.execute(
                "DELETE FROM owner_only_records WHERE canonical_table=? AND record_id=?",
                (canonical_table, record_id),
            )
            commit_connection(self.conn)
        return bool(cursor.rowcount)


def protection_fingerprint(conn: sqlite3.Connection) -> str:
    """Changes invalidate session artifacts even when the query/grant is unchanged."""
    from .blackhole import BlackholeStore

    state = {"entities": BlackholeStore(conn).list(), "records": RecordProtectionStore(conn).list()}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
