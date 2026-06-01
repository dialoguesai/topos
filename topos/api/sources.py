from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_api_key
from ..sources.registry import list_sources

router = APIRouter()


@router.get("/sources", dependencies=[Depends(require_api_key)])
async def get_sources() -> dict:
    return {"sources": [source.to_dict() for source in list_sources()]}
