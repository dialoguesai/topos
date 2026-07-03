"""Engine client: in-process or remote HTTP execution."""

from __future__ import annotations

import importlib.util
from typing import Optional, Protocol

import httpx

from .engine import Engine
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
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = task.model_dump(mode="json")
        with httpx.Client(timeout=self._timeout_sec) as client:
            resp = client.post(f"{self._base_url}/v1/engine/run", json=body, headers=headers)
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
