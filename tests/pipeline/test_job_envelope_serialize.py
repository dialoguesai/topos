"""JobEnvelope JSON round-trip and idempotency_key validation."""

import json

import pytest

from topos.pipeline.envelope import JobEnvelope, parse_envelope, serialize_envelope
from topos.pipeline.stages import PipelineStage


def test_job_envelope_json_round_trip() -> None:
    envelope = JobEnvelope(
        stage=PipelineStage.CANONICAL_MAP,
        source_id="chatgpt_file_ingestion",
        batch_id="batch-1",
        record_ids=["r1", "r2"],
        idempotency_key="chatgpt_file_ingestion:batch-1:canonical",
    )
    raw = serialize_envelope(envelope)
    parsed = parse_envelope(raw)
    assert parsed.stage == PipelineStage.CANONICAL_MAP
    assert parsed.source_id == "chatgpt_file_ingestion"
    assert parsed.batch_id == "batch-1"
    assert parsed.record_ids == ["r1", "r2"]
    assert parsed.idempotency_key == "chatgpt_file_ingestion:batch-1:canonical"
    assert json.loads(raw)["stage"] == "canonical_map"


def test_job_envelope_requires_idempotency_key() -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        JobEnvelope(
            stage=PipelineStage.RAW_WRITE,
            source_id="browser_visits",
            batch_id="b1",
            idempotency_key="",
        )
