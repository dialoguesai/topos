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
    assert CHATGPT_FILE.signal_derivation_jobs
    assert "embeddings" in CHATGPT_FILE.signal_derivation_jobs
