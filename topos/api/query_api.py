from __future__ import annotations

from fastapi import APIRouter

from ..analytics.duckdb_adapter import DuckDBAdapter

router = APIRouter()


@router.post("/query")
async def run_query() -> dict:
    _ = DuckDBAdapter
    return {"status": "stub"}
