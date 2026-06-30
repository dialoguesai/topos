"""Pipeline job envelopes for Wiki MVP (Phase 0 design + log-only stubs)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from .stages import PipelineStage

JobStatus = Literal["queued", "running", "completed", "failed"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobEnvelope:
    stage: PipelineStage
    source_id: str
    batch_id: str
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    record_ids: List[str] = field(default_factory=list)
    status: JobStatus = "queued"
    provenance: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    idempotency_key: str = ""
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["stage"] = self.stage.value
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobEnvelope":
        stage = data["stage"]
        if isinstance(stage, str):
            stage = PipelineStage(stage)
        return cls(
            job_id=str(data.get("job_id") or uuid.uuid4()),
            stage=stage,
            source_id=str(data["source_id"]),
            batch_id=str(data["batch_id"]),
            record_ids=list(data.get("record_ids") or []),
            status=data.get("status") or "queued",
            provenance=dict(data.get("provenance") or {}),
            error=data.get("error"),
            idempotency_key=str(data["idempotency_key"]),
            created_at=str(data.get("created_at") or _utc_now()),
        )


def serialize_envelope(envelope: JobEnvelope) -> str:
    return envelope.to_json()


def parse_envelope(raw: str | bytes | Dict[str, Any]) -> JobEnvelope:
    if isinstance(raw, dict):
        data = raw
    else:
        data = json.loads(raw)
    return JobEnvelope.from_dict(data)


def log_stage_transition(
    logger: Any,
    *,
    previous: PipelineStage,
    next_stage: PipelineStage,
    batch_id: str,
    source_id: str,
) -> None:
    logger.debug(
        "[PIPELINE:STAGE] %s -> %s source_id=%s batch_id=%s",
        previous.value,
        next_stage.value,
        source_id,
        batch_id,
    )
