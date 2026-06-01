"""Result formatter: raw adapter output + meta → ProcessingResult."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .tasks import ExecutionMeta, ProcessingResult, Provenance


def format_result(
    task_id: str,
    status: str,
    raw_output: Dict[str, Any],
    *,
    provenance_source_id: Optional[str] = None,
    provenance_record_ids: Optional[list] = None,
    execution_meta: Optional[ExecutionMeta] = None,
    error: Optional[str] = None,
    confidence: Optional[float] = None,
    output_type: str = "json",
) -> ProcessingResult:
    """Build a ProcessingResult from adapter output and metadata."""
    provenance = None
    if provenance_source_id is not None or (provenance_record_ids is not None and len(provenance_record_ids or []) > 0):
        provenance = Provenance(
            source_id=provenance_source_id,
            record_ids=provenance_record_ids or [],
        )
    return ProcessingResult(
        task_id=task_id,
        status=status,
        output=raw_output,
        output_type=output_type,
        confidence=confidence,
        provenance=provenance,
        execution_meta=execution_meta,
        error=error,
    )
