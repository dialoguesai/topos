"""
Gap: Post-ingest — fixed CANONICAL_JOBS → source signal_derivation_jobs wave
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.orchestrator import SignalDerivationOrchestrator
from topos.sources.registry import CHATGPT_FILE

pytestmark = pytest.mark.gap


def test_source_resolves_signal_jobs() -> None:
    orch = SignalDerivationOrchestrator()
    jobs = orch._resolve_job_names(CHATGPT_FILE.source_id, None)
    assert "embeddings" in jobs
    assert "entities" in jobs
