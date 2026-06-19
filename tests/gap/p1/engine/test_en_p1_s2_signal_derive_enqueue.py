"""
Gap: signal_derive — absent → post-canonical stub enqueue
Sprint: EN-P1-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import logging

import pytest

from topos.enrichment.orchestrator import EnrichmentOrchestrator
from topos.pipeline.stub_enqueue import enqueue_signal_derive_stub
from topos.pipeline.stages import PipelineStage

pytestmark = pytest.mark.gap


def test_signal_derive_stub_enqueue_and_orchestrator() -> None:
    logger = logging.getLogger("test.signal_derive")
    envelope = enqueue_signal_derive_stub(
        logger,
        source_id="chatgpt_file_ingestion",
        batch_id="batch-signal-1",
        record_ids=["msg-1"],
        signal_derivation_jobs=["embeddings"],
    )
    assert envelope.stage == PipelineStage.SIGNAL_DERIVE
    assert envelope.source_id == "chatgpt_file_ingestion"
    assert envelope.idempotency_key.endswith(":signal_derive")

    orchestrator = EnrichmentOrchestrator()
    result = orchestrator.enqueue_signal_derive_stub(
        source_id="chatgpt_file_ingestion",
        sync_batch_id="batch-signal-2",
        signal_derivation_jobs=["embeddings"],
    )
    assert result["status"] == "accepted"
    assert result["job_name"] == "signal_derive"
