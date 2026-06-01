from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.post("/sync")
async def trigger_sync() -> dict:
    return {"status": "stub"}
