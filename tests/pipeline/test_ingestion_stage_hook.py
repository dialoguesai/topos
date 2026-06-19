"""Post-canonical signal_derive stub emits structured log."""

import logging

from topos.pipeline.stub_enqueue import enqueue_signal_derive_stub
from topos.pipeline.stages import PipelineStage


def test_ingestion_stage_hook_emits_signal_derive_stub(caplog) -> None:
    logger = logging.getLogger("topos.ingestion.manager")
    with caplog.at_level(logging.INFO, logger="topos.ingestion.manager"):
        envelope = enqueue_signal_derive_stub(
            logger,
            source_id="chatgpt_file_ingestion",
            batch_id="batch-42",
            record_ids=["a", "b"],
            signal_derivation_jobs=["embeddings"],
        )
    assert envelope.stage == PipelineStage.SIGNAL_DERIVE
    assert any("[PIPELINE:SIGNAL_DERIVE]" in record.message for record in caplog.records)
    assert any("canonical_map -> signal_derive" in record.message for record in caplog.records)
