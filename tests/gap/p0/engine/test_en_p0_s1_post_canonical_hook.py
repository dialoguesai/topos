"""
Gap: Post-canonical hook — direct enrichment → log-only signal_derive stub
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import logging

import pytest

from topos.pipeline import enqueue_signal_derive_stub
from topos.pipeline.stages import PipelineStage

pytestmark = pytest.mark.gap


def test_post_canonical_hook_logs_signal_derive_stub(caplog) -> None:
    logger = logging.getLogger("topos.ingestion.manager")
    # Stage logs are DEBUG since 1.0.2 (5c4af45 quieted pipeline log noise).
    with caplog.at_level(logging.DEBUG, logger="topos.ingestion.manager"):
        envelope = enqueue_signal_derive_stub(
            logger,
            source_id="chatgpt_file_ingestion",
            batch_id="gap-batch",
            record_ids=["r1"],
        )
    assert envelope.stage == PipelineStage.SIGNAL_DERIVE
    assert any("[PIPELINE:SIGNAL_DERIVE]" in r.message for r in caplog.records)
