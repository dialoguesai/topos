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
            raise NotImplementedError("hosted_database adapters are Phase 4+")

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
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

        from ..db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)

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
