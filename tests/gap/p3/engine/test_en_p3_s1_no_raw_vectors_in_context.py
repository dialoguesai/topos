"""GT-EN-P3-S1-01f: No raw vector arrays in retrieval context."""

import json

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap


def _contains_float_vectors(obj) -> bool:
    if isinstance(obj, list) and obj and all(isinstance(x, (int, float)) for x in obj):
        return True
    if isinstance(obj, dict):
        if "vector" in obj or "embedding" in obj or "vectors" in obj:
            return True
        return any(_contains_float_vectors(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_float_vectors(v) for v in obj)
    return False


def test_inference_context_has_no_vector_arrays() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(manifest=availability_manifest(), access_mode="inference")
    )
    assert not _contains_float_vectors(result.context_packet)
    assert "vector" not in json.dumps(result.context_packet)
