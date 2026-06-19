"""
Gap: Vectors — float export possible → metadata-only API surface
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.features.signal.schemas import METADATA_EXCLUDE_KEYS, strip_vector_fields
from topos.features.signal.service import SignalService

from helpers import make_adapter_bundle

pytestmark = pytest.mark.gap


def test_list_metadata_strips_vector_payload() -> None:
    bundle = make_adapter_bundle()
    page = bundle.vector.list_metadata(limit=10)
    for item in page.items:
        stripped = strip_vector_fields(item)
        for key in METADATA_EXCLUDE_KEYS:
            assert key not in stripped


def test_signal_service_vectors_never_include_embedding() -> None:
    svc = SignalService(make_adapter_bundle())
    out = svc.list_vectors(limit=10)
    for item in out["items"]:
        assert "vector" not in item
        assert "embedding" not in item
        assert "vector_blob" not in item
