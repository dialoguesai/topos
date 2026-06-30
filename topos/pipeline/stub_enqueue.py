"""Log-only post-canonical signal_derive stub (Phase 0)."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from .envelope import JobEnvelope, log_stage_transition
from .stages import PipelineStage


def enqueue_signal_derive_stub(
    logger: logging.Logger,
    *,
    source_id: str,
    batch_id: str,
    record_ids: List[str],
    signal_derivation_jobs: Optional[List[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> JobEnvelope:
    """Emit a structured log envelope; does not enqueue work in Phase 0."""
    idempotency_key = f"{source_id}:{batch_id}:signal_derive"
    envelope = JobEnvelope(
        stage=PipelineStage.SIGNAL_DERIVE,
        source_id=source_id,
        batch_id=batch_id,
        record_ids=record_ids,
        status="queued",
        idempotency_key=idempotency_key,
        provenance={
            "signal_derivation_jobs": list(signal_derivation_jobs or []),
            **(metadata or {}),
        },
    )
    log_stage_transition(
        logger,
        previous=PipelineStage.CANONICAL_MAP,
        next_stage=PipelineStage.SIGNAL_DERIVE,
        batch_id=batch_id,
        source_id=source_id,
    )
    logger.debug(
        "[PIPELINE:SIGNAL_DERIVE] stub_enqueue job_id=%s source_id=%s batch_id=%s records=%d",
        envelope.job_id,
        source_id,
        batch_id,
        len(record_ids),
        extra={
            "stage": envelope.stage.value,
            "job_id": envelope.job_id,
            "idempotency_key": envelope.idempotency_key,
            "pipeline_envelope": envelope.to_dict(),
        },
    )
    return envelope
