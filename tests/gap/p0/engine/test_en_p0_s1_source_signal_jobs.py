"""
Gap: Source signal jobs — canonical_enrichment_jobs only → signal_derivation_jobs
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.sources.registry import CHATGPT_FILE

pytestmark = pytest.mark.gap


def test_chatgpt_source_lists_signal_derivation_jobs() -> None:
    from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    jobs = resolved_signal_derivation_jobs(CHATGPT_FILE)
    assert jobs
    assert "embeddings" in jobs
