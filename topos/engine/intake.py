"""Task intake: accept and normalize tasks."""

from __future__ import annotations

from typing import Any, Dict

from .tasks import ExecutionSpec, ModelRequest, ProcessingTask


def normalize_task(task: ProcessingTask) -> ProcessingTask:
    """Apply defaults to missing optional fields. Returns a copy with defaults set."""
    data = task.model_dump(mode="json")
    # Ensure execution has defaults
    if "execution" not in data or data["execution"] is None:
        data["execution"] = ExecutionSpec().model_dump(mode="json")
    else:
        exec_spec = data["execution"]
        if exec_spec.get("mode") is None:
            exec_spec["mode"] = "sync"
        if exec_spec.get("priority") is None:
            exec_spec["priority"] = 100
    # Ensure model_request has provider default
    if "model_request" in data and data["model_request"]:
        mr = data["model_request"]
        if mr.get("provider") is None or mr.get("provider") == "":
            mr["provider"] = "huggingface"
    return ProcessingTask.model_validate(data)


def task_from_dict(data: Dict[str, Any]) -> ProcessingTask:
    """Build ProcessingTask from dict (e.g. JSON payload)."""
    return ProcessingTask.model_validate(data)
