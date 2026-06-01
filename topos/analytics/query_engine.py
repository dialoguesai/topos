from __future__ import annotations

from typing import Any, Dict, List, Optional

from .duckdb_adapter import DuckDBAdapter


class QueryEngine:
    def __init__(self, adapter: DuckDBAdapter):
        self.adapter = adapter

    def query_messages_per_day(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT DATE(ts) as day, COUNT(*) as message_count
            FROM projection.messages
        """
        params: List[Any] = []
        if dataset_id:
            query += " WHERE dataset_id = ?"
            params.append(dataset_id)
        query += " GROUP BY day ORDER BY day DESC"
        return self.adapter.execute(query, params)

    def query_total_messages(self, dataset_id: Optional[str] = None) -> Dict[str, Any]:
        query = "SELECT COUNT(*) as total_messages FROM projection.messages"
        params: List[Any] = []
        if dataset_id:
            query += " WHERE dataset_id = ?"
            params.append(dataset_id)
        rows = self.adapter.execute(query, params)
        return rows[0] if rows else {"total_messages": 0}

    def query_messages_by_sender(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT sender_type, COUNT(*) as count
            FROM projection.messages
        """
        params: List[Any] = []
        if dataset_id:
            query += " WHERE dataset_id = ?"
            params.append(dataset_id)
        query += " GROUP BY sender_type ORDER BY count DESC"
        return self.adapter.execute(query, params)

    def query_avg_message_length(self, dataset_id: Optional[str] = None) -> Dict[str, Any]:
        query = """
            SELECT AVG(LENGTH(content)) as avg_length,
                   MIN(LENGTH(content)) as min_length,
                   MAX(LENGTH(content)) as max_length
            FROM projection.messages
        """
        params: List[Any] = []
        if dataset_id:
            query += " WHERE dataset_id = ?"
            params.append(dataset_id)
        rows = self.adapter.execute(query, params)
        if rows:
            row = rows[0]
            return {
                "avg_length": float(row.get("avg_length") or 0),
                "min_length": int(row.get("min_length") or 0),
                "max_length": int(row.get("max_length") or 0),
            }
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
