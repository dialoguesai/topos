from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..auth import require_api_key
from ..ingestion.reprocess import reprocess_source
from .ingestion_sources import ingest_source

router = APIRouter()


@router.post("/ingestion/start")
async def start_ingestion() -> dict:
    return {"status": "stub"}


@router.post("/ingestion/progress")
async def report_progress() -> dict:
    return {"status": "stub"}


@router.post("/ingestion/reprocess", dependencies=[Depends(require_api_key)])
async def ingestion_reprocess(payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return await reprocess_source(
            source_id=str(payload["source_id"]),
            dataset_id=str(payload["dataset_id"]),
            from_stage=payload.get("from_stage") or "raw",
            sync_batch_id=payload.get("sync_batch_id"),
            force=bool(payload.get("force", False)),
        )
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ingestion/audit", dependencies=[Depends(require_api_key)])
async def get_ingestion_audit(sync_batch_id: str) -> dict:
    from ..core.state import get_db_connection
    from ..pipeline.audit import SQLiteIngestAuditStore

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=503, detail="Database unavailable")
    store = SQLiteIngestAuditStore(conn)
    return {"sync_batch_id": sync_batch_id, "stages": store.query_by_batch(sync_batch_id)}


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
