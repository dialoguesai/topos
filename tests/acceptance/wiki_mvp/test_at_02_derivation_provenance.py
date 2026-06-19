"""
AT-2: Derivation jobs stamp provider/model provenance.
Profile: local engine
"""

import pytest

from topos.enrichment.job_writer import _merge_provenance

pytestmark = pytest.mark.acceptance


def test_at_02_derivation_provenance_fields() -> None:
    merged = _merge_provenance(
        {"record_id": "r1"},
        {"provider": "huggingface", "model": "embed-model", "job_id": "embeddings"},
    )
    assert merged["provider"] == "huggingface"
    assert merged["model"] == "embed-model"
    assert "provenance" in merged
