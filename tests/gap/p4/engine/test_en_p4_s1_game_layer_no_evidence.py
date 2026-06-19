"""
Gap: Game layer — evidence in result → minimum public_result only
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query.game_layer import DefaultGameLayer
from topos.query.types import FORBIDDEN_INFERENCE_PUBLIC_KEYS

pytestmark = pytest.mark.gap


def test_inference_public_result_excludes_evidence_trail() -> None:
    layer = DefaultGameLayer()
    result = layer.apply(
        context_packet={
            "scores": [{"value": 0.9, "confidence": 0.9}],
            "evidence": ["secret"],
            "source_rows": [{"id": 1}],
            "snippets": ["hidden"],
        },
        access_mode="inference",
        scope_id="availability:read",
    )
    payload = result.to_dict()
    for key in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
        assert key not in payload
