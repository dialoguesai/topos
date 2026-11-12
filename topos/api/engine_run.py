"""Engine task execution HTTP endpoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..auth import require_api_key
from ..engine.engine import Engine
from ..engine.admission import AdmissionRejected, EngineAdmission
from ..engine.tasks import ProcessingResult, ProcessingTask
from ..engine.overload import unavailable_from_detail
from ..engine.request_budget import RequestBudget, RequestStopped, budget_scope, await_budgeted, TIMEOUT_HEADER, MAX_SECONDS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def admission_lifespan(app):
    from ..config.settings import settings

    prior = getattr(app.state, "engine_http_admission", None)
    if prior is not None and prior.snapshot()["active"]:
        raise RuntimeError("Previous Engine HTTP workers are still draining")
    admission = EngineAdmission(active=settings.topos_engine_http_active,
                                waiting=settings.topos_engine_http_waiting,
                                queue_timeout=settings.topos_engine_http_queue_timeout_sec)
    app.state.engine_http_admission = admission
    try:
        yield
    finally:
        remaining = await asyncio.to_thread(admission.close, settings.topos_engine_http_shutdown_grace_sec)
        if remaining:
            logger.warning("Engine HTTP shutdown left %d local worker(s) draining; backend drain unknown", remaining)


router = APIRouter(tags=["engine-run"], lifespan=admission_lifespan)


def get_admission(request: Request) -> EngineAdmission:
    admission = getattr(request.app.state, "engine_http_admission", None)
    if admission is None:
        raise HTTPException(status_code=503, detail="Engine HTTP admission has not started")
    return admission


async def _execute_connected(request: Request, admission: EngineAdmission, task: ProcessingTask, budget=None) -> ProcessingResult:
    async def disconnected():
        # FastAPI has already consumed the JSON body. Listen for disconnect
        # without polling or treating the end of the request body as cancellation.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    def run():
        if budget is not None:
            budget.check()
        return Engine().run(task)

    async def execute():
        if budget is None:
            return await admission.execute(run)
        with budget_scope(budget):
            return await await_budgeted(admission.execute(run), budget)

    work = asyncio.create_task(execute())
    gone = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, gone), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        # execute() cancels a queued job, or detaches the caller while retaining
        # the active worker slot. The worker is never assumed to have stopped.
        if budget is not None:
            budget.cancel()
        raise HTTPException(status_code=499, detail="Caller disconnected")
    finally:
        for future in (work, gone):
            if not future.done():
                future.cancel()
        await asyncio.gather(work, gone, return_exceptions=True)


@router.post("/v1/engine/run", dependencies=[Depends(require_api_key)])
async def engine_run(body: Dict[str, Any], request: Request,
                     admission: EngineAdmission = Depends(get_admission)) -> Dict[str, Any]:
    """Await a bounded worker without blocking HTTP control requests."""
    try:
        task = ProcessingTask.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid ProcessingTask: {exc}",
        ) from exc
    budget = None
    value = request.headers.get(TIMEOUT_HEADER)
    if value is not None:
        if not value.isascii() or not value.isdecimal() or len(value) > 6 or not 1 <= int(value) <= MAX_SECONDS * 1000:
            raise HTTPException(status_code=422, detail="Invalid compute request budget")
        budget = RequestBudget(int(value) / 1000)
    try:
        result = await _execute_connected(request, admission, task, budget)
    except RequestStopped as exc:
        result = ProcessingResult(task_id=task.id, status="deferred", error=str(exc),
                                  output={"availability": exc.detail()})
    except AdmissionRejected as exc:
        from ..observability.metrics import measure
        measure("counter", "topos.compute.rejections", 1, unit="{request}",
                attributes={"topos.compute.boundary": "engine_http_admission", "error.type": exc.code,
                            "topos.admission.reason": exc.reason})
        result = ProcessingResult(task_id=task.id, status="deferred", error=str(exc),
            output={"availability": exc.detail(), "admission": {
                "boundary": "engine_http_workers_per_process", "reason": exc.reason}})
    if result.status == "deferred":
        for code in (429, 503):
            availability = unavailable_from_detail(code, result.output.get("availability"))
            if availability is not None:
                return JSONResponse(status_code=code, headers=availability.headers(),
                                    content={"result": result.model_dump(mode="json")})
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


@router.get("/v1/engine/admission", dependencies=[Depends(require_api_key)])
async def engine_admission(admission: EngineAdmission = Depends(get_admission)) -> Dict[str, Any]:
    """Content-free local worker accounting; does not claim provider drain."""
    return admission.snapshot()
