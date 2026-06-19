"""
Gap: Parity — untested → equivalent metadata keys local vs hosted fake
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.storage.adapters.fakes import InMemoryVectorIndex

pytestmark = pytest.mark.gap


def test_vector_metadata_shape_consistent() -> None:
    idx = InMemoryVectorIndex()
    idx.upsert(
        {
            "embedding_id": "e1",
            "record_id": "r1",
            "source_id": "chatgpt",
            "signal_dimension": "memory",
            "model": "mini",
            "provider": "huggingface",
            "dims": 3,
        },
        vector=[0.1, 0.2, 0.3],
    )
    item = idx.list_metadata().items[0]
    for key in ("embedding_id", "record_id", "source_id", "signal_dimension", "model", "provider", "dims"):
        assert key in item
    assert "vector" not in item
