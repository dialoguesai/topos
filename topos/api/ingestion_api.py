from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ..auth import require_api_key
from .ingestion_sources import ingest_source

router = APIRouter()


@router.post("/ingestion/start")
async def start_ingestion() -> dict:
    return {"status": "stub"}


@router.post("/ingestion/progress")
async def report_progress() -> dict:
    return {"status": "stub"}


@router.post("/ingestion/upload", dependencies=[Depends(require_api_key)])
async def upload_ingestion_file(request: Request) -> dict:
    return await ingest_source("chatgpt_file_ingestion", request)


@router.post("/ingestion/upload-local-path", dependencies=[Depends(require_api_key)])
async def upload_ingestion_local_path(
    request: Request, payload: dict = Body(default_factory=dict)
) -> dict:
    dataset_id = payload.get("dataset_id")
    file_path = payload.get("file_path")
    return await ingest_source(
        "chatgpt_file_ingestion",
        request,
        dataset_id=dataset_id,
        file_path=file_path,
    )
