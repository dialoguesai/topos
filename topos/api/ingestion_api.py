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
    """Re-run raw→canonical for a source (optional newest-N via ``limit``).

    Body:
      - source_id (required)
      - dataset_id (required)
      - from_stage: ``raw`` (default) | ``canonical``
      - limit: optional int — newest N raw rows only
      - run_enrichment: optional bool (default true)
      - force / sync_batch_id: optional
    """
    try:
        limit_raw = payload.get("limit")
        limit = int(limit_raw) if limit_raw is not None and str(limit_raw).strip() != "" else None
        return await reprocess_source(
            source_id=str(payload["source_id"]),
            dataset_id=str(payload["dataset_id"]),
            from_stage=payload.get("from_stage") or "raw",
            sync_batch_id=payload.get("sync_batch_id"),
            force=bool(payload.get("force", False)),
            limit=limit,
            run_enrichment=bool(payload.get("run_enrichment", True)),
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


@router.get("/ingestion/local-exports", dependencies=[Depends(require_api_key)])
async def list_local_exports() -> dict:
    """Exports sitting on this machine, for the import screen to offer.

    The node is the only party that can answer this: a browser cannot read a
    filesystem, and cannot produce an absolute path even for a file the user
    picks. So the node looks, and hands back opaque handles plus labels a person
    can recognise -- never the paths themselves.

    Bounded and stateless by construction (see ``local_exports``): a few known
    folders, shallow, symlinks skipped, time-boxed, nothing written down.
    """
    import asyncio

    from ..ingestion.local_exports import find_exports

    result = await asyncio.to_thread(find_exports)
    return result.as_payload()


@router.post("/ingestion/upload-local-path", dependencies=[Depends(require_api_key)])
async def upload_ingestion_local_path(
    request: Request, payload: dict = Body(default_factory=dict)
) -> dict:
    """Ingest a file that is already on this machine. No bytes cross a network.

    Accepts either a ``handle`` from ``/ingestion/local-exports`` -- the path
    never leaves the node -- or an explicit ``file_path``, which is still how
    someone points at an export outside the searched folders.
    """
    from ..ingestion.local_exports import resolve

    dataset_id = payload.get("dataset_id")
    file_path = payload.get("file_path")
    handle = str(payload.get("handle") or "").strip()

    if handle and not file_path:
        resolved = resolve(handle)
        if resolved is None:
            # Handles are re-found rather than remembered, so a stale one means
            # the file moved or went away -- not that the user did anything odd.
            raise HTTPException(
                status_code=404,
                detail="That export is no longer where we found it. Scan again and pick it.",
            )
        file_path = str(resolved)

    if not file_path:
        raise HTTPException(status_code=400, detail="Provide a handle or a file_path.")

    return await ingest_source(
        "chatgpt_file_ingestion",
        request,
        dataset_id=dataset_id,
        file_path=file_path,
    )
