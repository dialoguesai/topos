"""
Gap: Job catalog — emo_27 only → all §6.3 job IDs registered and dispatchable
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.jobs import SIGNAL_JOB_REGISTRY

pytestmark = pytest.mark.gap

EXPECTED_JOBS = {
    "emo_27",
    "entities",
    "embeddings",
    "topics",
    "sentiment",
    "dimension_summary",
    "goal_extraction",
    "relationship_edges",
    "availability_scores",
}


def test_all_mvp_jobs_registered() -> None:
    assert EXPECTED_JOBS.issubset(set(SIGNAL_JOB_REGISTRY.keys()))
