"""Topos Engine: unified runtime for ML/LLM processing (enrichments, transformations, queries)."""

from .engine import Engine
from .client import EngineClient, LocalEngineClient, RemoteEngineClient, get_engine_client, get_engine_client_or_local
from .model_cache import ModelSlot, get_model_cache
from .tasks import (
    ExecutionMeta,
    ExecutionSpec,
    ModelRequest,
    ProcessingResult,
    ProcessingTask,
    Provenance,
    RequestedBy,
    TaskOptions,
    build_task,
    build_url_classification_task,
)

__all__ = [
    "Engine",
    "EngineClient",
    "LocalEngineClient",
    "RemoteEngineClient",
    "get_engine_client",
    "get_engine_client_or_local",
    "ModelSlot",
    "get_model_cache",
    "ProcessingTask",
    "ProcessingResult",
    "ModelRequest",
    "ExecutionSpec",
    "TaskOptions",
    "RequestedBy",
    "Provenance",
    "ExecutionMeta",
    "build_task",
    "build_url_classification_task",
]
