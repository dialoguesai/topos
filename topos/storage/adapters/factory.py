"""Adapter factory for Wiki MVP storage backends."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from .fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)
from .protocols import (
    AuditLogStore,
    CanonicalStore,
    GraphEdgeStore,
    QuerySessionStore,
    SignalFeatureStore,
    VectorIndex,
)
from .sqlite.stores import (
    SQLiteAuditLogStore,
    SQLiteCanonicalStore,
    SQLiteGraphEdgeStore,
    SQLiteQuerySessionStore,
    SQLiteSignalFeatureStore,
    SQLiteVectorIndex,
)

BackendKind = Literal["local_database", "hosted_database", "memory"]


def _is_sqlite_conn(conn: Any) -> bool:
    module_name = conn.__class__.__module__.lower()
    return "sqlite" in module_name


@dataclass(frozen=True)
class AdapterBundle:
    canonical: CanonicalStore
    signal: SignalFeatureStore
    vector: VectorIndex
    graph: GraphEdgeStore
    audit: AuditLogStore
    query_session: QuerySessionStore
    backend: BackendKind


class AdapterFactory:
    """Constructs storage adapter bundles for local SQLite or in-memory fakes."""

    @staticmethod
    def create(
        backend: BackendKind = "local_database",
        *,
        db_path: Optional[str | Path] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> AdapterBundle:
        if backend == "hosted_database":
            import os

            pg_conn = conn
            if pg_conn is None:
                try:
                    from ..db.postgres import connect_postgres

                    pg_conn = connect_postgres()
                except Exception:
                    pg_conn = None
            if pg_conn is not None and not _is_sqlite_conn(pg_conn) and os.environ.get("POSTGRES_URL"):
                from .postgres.stores import build_postgres_adapter_bundle

                return build_postgres_adapter_bundle(pg_conn)
            # Contract tests without Postgres: sqlite bundle tagged hosted_database.
            if conn is None:
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
            from ..db.migrations import ensure_migrations_applied

            ensure_migrations_applied(conn)
            bundle = AdapterFactory.create("local_database", conn=conn)
            return AdapterBundle(
                canonical=bundle.canonical,
                signal=bundle.signal,
                vector=bundle.vector,
                graph=bundle.graph,
                audit=bundle.audit,
                query_session=bundle.query_session,
                backend="hosted_database",
            )

        if backend == "memory":
            return AdapterBundle(
                canonical=InMemoryCanonicalStore(),
                signal=InMemorySignalFeatureStore(),
                vector=InMemoryVectorIndex(),
                graph=InMemoryGraphEdgeStore(),
                audit=InMemoryAuditLogStore(),
                query_session=InMemoryQuerySessionStore(),
                backend=backend,
            )

        if conn is None:
            if db_path is None:
                raise ValueError("db_path or conn required for local_database backend")
            # Prefer the process singleton when the path matches, so we do not
            # open a second untuned writer against the same SQLite file.
            try:
                from ...core.state import get_db_connection

                shared = get_db_connection()
                if shared is not None:
                    try:
                        row = shared.execute("PRAGMA database_list").fetchone()
                        shared_file = str(row[2] or "").strip() if row else ""
                        if shared_file and str(Path(shared_file).resolve()) == str(Path(db_path).resolve()):
                            conn = shared
                    except (sqlite3.Error, OSError, RuntimeError):
                        pass
            except Exception:
                pass
            if conn is None:
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row

        # Tune before migrations so vector_storage_v4 sees the vec extension
        # and can create/backfill the ANN table.
        from ..db.connection_tuning import tune_connection

        tune_connection(conn)

        from ..db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)
        from ..canonical.ai_chat.tables import CanonicalTablesManager

        CanonicalTablesManager(conn)

        return AdapterBundle(
            canonical=SQLiteCanonicalStore(conn),
            signal=SQLiteSignalFeatureStore(conn),
            vector=SQLiteVectorIndex(conn),
            graph=SQLiteGraphEdgeStore(conn),
            audit=SQLiteAuditLogStore(conn),
            query_session=SQLiteQuerySessionStore(conn),
            backend=backend,
        )

    @classmethod
    def from_runtime(
        cls,
        profile: Dict[str, Any] | None = None,
        *,
        db_path: Optional[str | Path] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> AdapterBundle:
        """Build adapters from runtime profile (`database_hosting_mode`)."""
        mode = (profile or {}).get("database_hosting_mode", "local_database")
        if mode in ("hosted_database", "memory"):
            return cls.create(mode)
        if conn is None and db_path is None:
            from ...core.state import get_db_connection

            conn = get_db_connection()
        return cls.create("local_database", db_path=db_path, conn=conn)
