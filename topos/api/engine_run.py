"""Engine task execution HTTP endpoint."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_api_key
from ..engine.engine import Engine
from ..engine.tasks import ProcessingResult, ProcessingTask

router = APIRouter(tags=["engine-run"])


@router.post("/v1/engine/run", dependencies=[Depends(require_api_key)])
async def engine_run(body: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a ProcessingTask synchronously and return ProcessingResult JSON."""
    try:
        task = ProcessingTask.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid ProcessingTask: {exc}",
        ) from exc
    result: ProcessingResult = Engine().run(task)
    return {"result": result.model_dump(mode="json")}


@router.get("/v1/engine/memory", dependencies=[Depends(require_api_key)])
async def engine_memory() -> Dict[str, Any]:
    from ..engine.memory_utils import get_process_rss_mb, mps_available, torch_available
    from ..engine.model_cache import get_model_cache

    cache = get_model_cache()
    return {
        "rss_mb": get_process_rss_mb(),
        "resident_slots": cache.resident_slots(),
        "max_resident": cache.max_resident,
        "evictions_total": cache.evictions_total,
        "torch_available": torch_available(),
        "mps_available": mps_available(),
    }
