"""DuckDB adapter for analytics queries."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore

logger = logging.getLogger("topos.analytics.duckdb")


class DuckDBAdapter:
    def __init__(self, db_path: Optional[Path] = None):
        if duckdb is None:
            raise ImportError("duckdb package not installed")
        self.conn = duckdb.connect(str(db_path) if db_path else ":memory:")

    def attach_sqlite(self, sqlite_path: str) -> None:
        escaped_path = sqlite_path.replace("'", "''")
        self.conn.execute(f"ATTACH '{escaped_path}' AS projection (TYPE SQLITE)")

    def query_jsonl_file(
        self,
        file_path: str,
        dataset_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        escaped_path = file_path.replace("'", "''")
        query = f"SELECT * FROM read_ndjson('{escaped_path}')"
        if dataset_id:
            query += f" WHERE dataset_id = '{dataset_id}'"
        result = self.conn.execute(query).fetchall()
        columns = [desc[0] for desc in self.conn.description] if self.conn.description else []
        return [dict(zip(columns, row)) for row in result]

    def execute(self, query: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        params = params or []
        result = self.conn.execute(query, params).fetchall()
        columns = [desc[0] for desc in self.conn.description] if self.conn.description else []
        return [dict(zip(columns, row)) for row in result]

    def close(self) -> None:
        if self.conn:
            self.conn.close()
