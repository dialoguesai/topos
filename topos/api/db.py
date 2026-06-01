from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/db/status")
async def db_status() -> dict:
    return {"status": "stub", "database": "unknown"}
