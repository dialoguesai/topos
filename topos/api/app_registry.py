from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from ..auth import require_api_key

router = APIRouter()


@router.get("/apps", dependencies=[Depends(require_api_key)])
async def list_apps() -> Dict[str, Any]:
    return {"status": "stub", "apps": []}


@router.post("/apps", dependencies=[Depends(require_api_key)])
async def create_app(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    return {"status": "stub", "app": payload}


@router.get("/apps/{app_id}/sources", dependencies=[Depends(require_api_key)])
async def list_app_sources(app_id: str) -> Dict[str, Any]:
    return {"status": "stub", "app_id": app_id, "sources": []}


@router.post("/apps/{app_id}/sources", dependencies=[Depends(require_api_key)])
async def create_app_source(
    app_id: str, payload: Dict[str, Any] = Body(default_factory=dict)
) -> Dict[str, Any]:
    return {"status": "stub", "app_id": app_id, "source": payload}
