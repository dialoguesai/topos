from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ..auth import require_api_key
from .ingestion_sources import ingest_source

router = APIRouter()


@router.post("/store_message", dependencies=[Depends(require_api_key)])
async def store_message(
    request: Request, payload: dict = Body(default_factory=dict)
) -> dict:
    dataset_id = payload.get("dataset_id")
    return await ingest_source(
        "chatgpt_ui_conversation",
        request,
        dataset_id=dataset_id,
        payload=payload,
    )
