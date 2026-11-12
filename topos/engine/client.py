"""Engine client: in-process or remote HTTP execution."""

from __future__ import annotations

import importlib.util
from typing import Optional, Protocol

import httpx

from .engine import Engine
from .overload import unavailable_from_detail
from .tasks import ProcessingResult, ProcessingTask


class EngineClient(Protocol):
    def run(self, task: ProcessingTask) -> ProcessingResult: ...


class LocalEngineClient:
    """Runs tasks in-process via Engine.run()."""

    def __init__(self, engine: Optional[Engine] = None) -> None:
        self._engine = engine or Engine()

    @property
    def engine(self) -> Engine:
        return self._engine

    def run(self, task: ProcessingTask) -> ProcessingResult:
        return self._engine.run(task)


class RemoteEngineClient:
    """POST ProcessingTask JSON to a remote Topos Engine service."""

    def __init__(self, base_url: str, *, api_key: Optional[str] = None, timeout_sec: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = (api_key or "").strip() or None
        self._timeout_sec = timeout_sec

    def run(self, task: ProcessingTask) -> ProcessingResult:
        from opentelemetry.trace import SpanKind
        from ..observability.tracing import start_span, mark_error

        with start_span("topos.engine.remote", kind=SpanKind.CLIENT):
            result = self._run(task)
            if result.status != "completed":
                availability = result.output.get("availability")
                error = unavailable_from_detail(503, availability) or unavailable_from_detail(429, availability)
                mark_error(error.code if error else "engine_task_failed")
            return result

    def _run(self, task: ProcessingTask) -> ProcessingResult:
        from ..observability.tracing import inject_context

        headers = {"Content-Type": "application/json"}
        headers.update(inject_context())
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = task.model_dump(mode="json")
        from .request_budget import current_budget, request, TIMEOUT_HEADER
        budget = current_budget()
        if budget is not None:
            headers[TIMEOUT_HEADER] = str(max(1, int(budget.remaining() * 1000)))
            resp = request("POST", f"{self._base_url}/v1/engine/run", json=body,
                           headers=headers, timeout=self._timeout_sec)
        else:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(f"{self._base_url}/v1/engine/run", json=body, headers=headers)
        if resp.status_code in (429, 503):
            # Only our typed deferred result has Engine semantics. Generic
            # proxy failures and policy refusals remain HTTP errors.
            try:
                deferred = ProcessingResult.model_validate(resp.json()["result"])
                availability = unavailable_from_detail(resp.status_code, deferred.output.get("availability"))
                if deferred.task_id == task.id and deferred.status == "deferred" and availability is not None:
                    return deferred
            except (ValueError, KeyError, TypeError):
                pass
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        return ProcessingResult.model_validate(data)


def ml_deps_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def get_engine_client(engine: Optional[Engine] = None) -> EngineClient:
    """Return remote client when configured or ML deps missing; else local."""
    from ..config.settings import settings

    remote_url = (getattr(settings, "topos_engine_service_url", None) or "").strip()
    if remote_url:
        return RemoteEngineClient(
            remote_url,
            api_key=getattr(settings, "topos_key", None),
        )
    if not ml_deps_available():
        raise RuntimeError(
            "ML dependencies unavailable and TOPOS_ENGINE_SERVICE_URL is not configured"
        )
    return LocalEngineClient(engine)


def get_engine_client_or_local(engine: Optional[Engine] = None) -> EngineClient:
    """Like get_engine_client but always falls back to local when torch is present."""
    from ..config.settings import settings

    remote_url = (getattr(settings, "topos_engine_service_url", None) or "").strip()
    if remote_url:
        return RemoteEngineClient(
            remote_url,
            api_key=getattr(settings, "topos_key", None),
        )
    return LocalEngineClient(engine)
