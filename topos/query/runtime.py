"""Runtime wiring for query pipeline (DB-backed adapters)."""

from __future__ import annotations

from typing import Optional

import sqlite3

from ..storage.adapters.factory import AdapterFactory, AdapterBundle
from .pipeline import QueryPipelineOrchestrator


def adapter_bundle_for_conn(conn: Optional[sqlite3.Connection]) -> AdapterBundle:
    if conn is not None:
        return AdapterFactory.create("local_database", conn=conn)
    return AdapterFactory.from_runtime({"database_hosting_mode": "memory"})


def get_query_orchestrator(conn: Optional[sqlite3.Connection] = None) -> QueryPipelineOrchestrator:
    if conn is None:
        from ..core.state import get_db_connection

        conn = get_db_connection()
    return QueryPipelineOrchestrator(adapters=adapter_bundle_for_conn(conn))
