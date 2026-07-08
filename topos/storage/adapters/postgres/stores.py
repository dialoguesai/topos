"""Postgres-backed adapter bundle for hosted_database profile."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..protocols import ListPage
from ...canonical.postgres import PostgresCanonicalStore as TypedPostgresCanonicalStore
from ..sqlite.stores import (
    _NATIVE_ID_COL,
    _NATIVE_TABLES,
    SQLiteAuditLogStore,
    SQLiteCanonicalStore,
    SQLiteGraphEdgeStore,
    SQLiteQuerySessionStore,
    SQLiteSignalFeatureStore,
    SQLiteVectorIndex,
)


class PostgresCanonicalAdapter:
    _NATIVE_TABLES = _NATIVE_TABLES

    def __init__(self, conn) -> None:
        self._conn = conn
        self._typed = TypedPostgresCanonicalStore(conn)

    def upsert(self, table: str, record: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str:
        ref = self._typed.upsert(table, record, sync_batch_id=idempotency_key)
        return ref.record_id

    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        if table not in self._NATIVE_TABLES:
            return None
        id_col = _NATIVE_ID_COL[table]
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {table} WHERE {id_col}=%s", (record_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            cur.close()

    def list(
        self,
        table: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        disclosure_tier: str = "owner_raw",
        contains: Optional[List[str]] = None,
    ) -> ListPage:
        _ = disclosure_tier
        if table not in self._NATIVE_TABLES:
            return ListPage(items=[], total=0, offset=offset, limit=limit)
        params: List[Any] = []
        where = ""
        if source_id is not None:
            where = " WHERE source_id=%s"
            params.append(source_id)
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}{where}", tuple(params))
            total = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT * FROM {table}{where} ORDER BY 1 LIMIT %s OFFSET %s",
                tuple([*params, limit, offset]),
            )
            cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
            tokens = [str(t).strip().lower() for t in (contains or []) if str(t).strip()]
            if tokens:
                # Page-level filter only (parity stopgap — the sqlite adapter
                # filters in SQL over the full table).
                items = [
                    r for r in items
                    if any(t in " ".join(str(v) for v in r.values()).lower() for t in tokens)
                ]
            return ListPage(items=items, total=total, offset=offset, limit=limit)
        finally:
            cur.close()

    def delete(self, table: str, record_id: str) -> bool:
        if table not in self._NATIVE_TABLES:
            return False
        id_col = _NATIVE_ID_COL[table]
        cur = self._conn.cursor()
        try:
            cur.execute(f"DELETE FROM {table} WHERE {id_col}=%s", (record_id,))
            self._conn.commit()
            return cur.rowcount > 0
        finally:
            cur.close()

    def count(self, table: str, *, source_id: Optional[str] = None) -> int:
        return self.list(table, source_id=source_id, limit=1, offset=0).total


def build_postgres_adapter_bundle(conn):
    """Hosted bundle: Postgres canonical + signal stores on the same connection."""
    from ..factory import AdapterBundle, _is_sqlite_conn

    if _is_sqlite_conn(conn):
        from ..db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)
        return AdapterBundle(
            canonical=SQLiteCanonicalStore(conn),
            signal=SQLiteSignalFeatureStore(conn),
            vector=SQLiteVectorIndex(conn),
            graph=SQLiteGraphEdgeStore(conn),
            audit=SQLiteAuditLogStore(conn),
            query_session=SQLiteQuerySessionStore(conn),
            backend="hosted_database",
        )
    return AdapterBundle(
        canonical=PostgresCanonicalAdapter(conn),
        signal=SQLiteSignalFeatureStore(conn),
        vector=SQLiteVectorIndex(conn),
        graph=SQLiteGraphEdgeStore(conn),
        audit=SQLiteAuditLogStore(conn),
        query_session=SQLiteQuerySessionStore(conn),
        backend="hosted_database",
    )


