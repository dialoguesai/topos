from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.post("/backup")
async def backup_database() -> dict:
    return {"status": "stub"}


@router.post("/restore")
async def restore_database() -> dict:
    return {"status": "stub"}
