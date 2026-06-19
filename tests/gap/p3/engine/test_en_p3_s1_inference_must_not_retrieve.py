"""GT-EN-P3-S1-01e: Inference must_not_retrieve enforcement."""

import json

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap


def test_availability_inference_strips_forbidden_fields() -> None:
    bundle = make_adapter_bundle()
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(
        RetrievalRequest(manifest=availability_manifest(), access_mode="inference")
    )
    blob = json.dumps(result.context_packet)
    assert "Secret meeting" not in blob
    assert "should not appear" not in blob
