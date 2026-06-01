from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from ..analytics.duckdb_adapter import DuckDBAdapter
from ..analytics.profiles import get_profile
from ..analytics.query_engine import QueryEngine

router = APIRouter()


@router.get("/analytics")
async def get_analytics_endpoint(
    query: Optional[str] = Query(None, description="Analytics query name"),
    profile_id: Optional[str] = Query(None, description="Analytics profile id"),
    dataset_id: Optional[str] = Query(None),
) -> dict:
    adapter = DuckDBAdapter()
    engine = QueryEngine(adapter)

    if profile_id:
        profile = get_profile(profile_id)
        if not profile:
            return {"status": "error", "error": "unknown profile_id"}
        results = {}
        for item in profile["queries"]:
            try:
                if item == "messages_per_day":
                    results[item] = engine.query_messages_per_day(dataset_id=dataset_id)
                elif item == "total_messages":
                    results[item] = engine.query_total_messages(dataset_id=dataset_id)
                elif item == "messages_by_sender":
                    results[item] = engine.query_messages_by_sender(dataset_id=dataset_id)
                elif item == "avg_message_length":
                    results[item] = engine.query_avg_message_length(dataset_id=dataset_id)
                else:
                    results[item] = {"error": "unsupported query"}
            except Exception:
                results[item] = []
        return {"profile_id": profile_id, "results": results}

    if query == "messages_per_day":
        return {"query": query, "result": engine.query_messages_per_day(dataset_id=dataset_id)}
    if query == "total_messages":
        return {"query": query, "result": engine.query_total_messages(dataset_id=dataset_id)}
    if query == "messages_by_sender":
        return {"query": query, "result": engine.query_messages_by_sender(dataset_id=dataset_id)}
    if query == "avg_message_length":
        return {"query": query, "result": engine.query_avg_message_length(dataset_id=dataset_id)}
    return {"status": "stub", "query": query, "dataset_id": dataset_id}
