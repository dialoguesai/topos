"""
Gap: Pipeline stages — implicit logs → PipelineStage enum + envelope JSON
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.pipeline import JobEnvelope, PipelineStage, parse_envelope, serialize_envelope

pytestmark = pytest.mark.gap


def test_pipeline_stages_and_envelope_round_trip() -> None:
    assert PipelineStage.SIGNAL_DERIVE.value == "signal_derive"
    envelope = JobEnvelope(
        stage=PipelineStage.SIGNAL_DERIVE,
        source_id="chatgpt_file_ingestion",
        batch_id="batch-1",
        idempotency_key="chatgpt_file_ingestion:batch-1:signal_derive",
    )
    restored = parse_envelope(serialize_envelope(envelope))
    assert restored.stage == PipelineStage.SIGNAL_DERIVE
