"""
Gap: Provenance — partial columns → all MVP derivation jobs stamped
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.job_writer import _merge_provenance

pytestmark = pytest.mark.gap


@pytest.mark.parametrize(
    "job_name",
    ["entities", "embeddings", "dimension_summary", "relationship_edges"],
)
def test_derivation_provenance_includes_provider_model(job_name: str) -> None:
    provenance = {"provider": "huggingface", "model": "test-model", "job_id": job_name}
    merged = _merge_provenance({"record_id": "r1"}, provenance)
    assert merged["provider"] == "huggingface"
    assert merged["model"] == "test-model"
    assert merged["provenance"]["job_id"] == job_name
