"""GT-EN-P3-S1-03b: Inference public_result has no evidence trail."""

import pytest

from topos.query.game_layer import DefaultGameLayer
from topos.query.types import FORBIDDEN_INFERENCE_PUBLIC_KEYS

pytestmark = pytest.mark.gap


def test_inference_public_result_excludes_evidence_keys() -> None:
    layer = DefaultGameLayer()
    result = layer.apply(
        context_packet={
            "scores": [{"value": 0.9, "confidence": 0.9}],
            "evidence": ["secret"],
            "source_rows": [{"id": 1}],
            "retrieval_context": "hidden",
        },
        access_mode="inference",
        scope_id="availability:read",
    )
    payload = result.to_dict()
    for key in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
        assert key not in payload
