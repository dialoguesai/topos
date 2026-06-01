"""Task validator: reject invalid tasks."""

from __future__ import annotations

from typing import Optional, Tuple

from .tasks import ProcessingTask


def validate_task(task: ProcessingTask) -> Tuple[bool, Optional[str]]:
    """
    Validate required fields and obvious invariants.
    Returns (is_valid, error_message).
    """
    if not task.id or not str(task.id).strip():
        return False, "task id is required and must be non-empty"
    if not task.type or not str(task.type).strip():
        return False, "task type is required and must be non-empty"
    if not task.model_request:
        return False, "model_request is required"
    if not task.model_request.provider or not str(task.model_request.provider).strip():
        return False, "model_request.provider is required"
    return True, None
