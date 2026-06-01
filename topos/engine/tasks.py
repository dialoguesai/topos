"""Task contract for the Topos Engine (PRD §6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --- Nested structures (PRD §6.1) ---


class ModelRequest(BaseModel):
    """Model selection for a task."""

    provider: str = Field(..., description="e.g. ollama, huggingface")
    model: Optional[str] = Field(None, description="Model name or id; default from registry if omitted")


class ExecutionSpec(BaseModel):
    """Execution mode and scheduling hints."""

    mode: str = Field(default="sync", description="sync, async, batch, etc.")
    priority: int = Field(default=100, description="Lower = higher priority")
    batch_key: Optional[str] = Field(None, description="Group tasks for model-aware batching")


class TaskOptions(BaseModel):
    """Per-task options."""

    store_result: bool = Field(default=True, description="Whether to persist result")
    apply_fisher_filter: bool = Field(default=False, description="Apply Fisher filter at output")


class RequestedBy(BaseModel):
    """Who requested the task and from where."""

    user_id: Optional[str] = None
    origin: Optional[str] = Field(None, description="e.g. write_event, batch, manual")


# --- ProcessingTask (PRD §6.1) ---


class ProcessingTask(BaseModel):
    """Input contract for the Engine. All work is represented as a task."""

    id: str = Field(..., description="Unique task id")
    type: str = Field(..., description="enrichment, transformation, derivation, query, etc.")
    subtype: Optional[str] = Field(None, description="e.g. emotion_classification, url_classification")
    source_id: Optional[str] = None
    record_ids: List[str] = Field(default_factory=list)
    input: Dict[str, Any] = Field(default_factory=dict)
    model_request: ModelRequest = Field(...)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    options: TaskOptions = Field(default_factory=TaskOptions)
    requested_by: Optional[RequestedBy] = None
    created_at: Optional[str] = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    model_config = {"extra": "forbid"}

    def model_dump_json_roundtrip(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict (for transport and tests)."""
        return self.model_dump(mode="json")


# --- ProcessingResult (PRD §6.2) ---


class Provenance(BaseModel):
    """Provenance of the result."""

    source_id: Optional[str] = None
    record_ids: List[str] = Field(default_factory=list)


class ExecutionMeta(BaseModel):
    """Execution metadata."""

    provider: Optional[str] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    cache_hit: bool = False


class ProcessingResult(BaseModel):
    """Output contract for the Engine."""

    task_id: str = Field(...)
    status: str = Field(..., description="completed, failed, error, etc.")
    output: Dict[str, Any] = Field(default_factory=dict)
    output_type: str = Field(default="json")
    confidence: Optional[float] = None
    provenance: Optional[Provenance] = None
    execution_meta: Optional[ExecutionMeta] = None
    error: Optional[str] = None

    model_config = {"extra": "forbid"}

    def model_dump_json_roundtrip(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return self.model_dump(mode="json")


# --- Helper ---


def build_task(
    task_id: str,
    task_type: str,
    model_request: ModelRequest,
    *,
    subtype: Optional[str] = None,
    source_id: Optional[str] = None,
    record_ids: Optional[List[str]] = None,
    input_data: Optional[Dict[str, Any]] = None,
    execution: Optional[ExecutionSpec] = None,
    requested_by: Optional[RequestedBy] = None,
) -> ProcessingTask:
    """Build a ProcessingTask with required fields and optional overrides."""
    return ProcessingTask(
        id=task_id,
        type=task_type,
        subtype=subtype,
        source_id=source_id,
        record_ids=record_ids or [],
        input=input_data or {},
        model_request=model_request,
        execution=execution or ExecutionSpec(),
        requested_by=requested_by,
    )


def build_url_classification_task(
    task_id: str,
    url: str,
    title: Optional[str] = None,
    *,
    source_id: Optional[str] = None,
    record_ids: Optional[List[str]] = None,
) -> ProcessingTask:
    """Build a ProcessingTask for URL classification (enrichment, url_classification)."""
    return build_task(
        task_id=task_id,
        task_type="enrichment",
        model_request=ModelRequest(provider="huggingface"),
        subtype="url_classification",
        source_id=source_id,
        record_ids=record_ids or [],
        input_data={"url": url, "title": title or ""},
    )
