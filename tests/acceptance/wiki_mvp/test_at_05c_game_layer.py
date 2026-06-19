"""
AT-5c: Game layer minimum reveal for Inference mode.
Profile: local
"""

import pytest

from topos.query.game_layer import DefaultGameLayer
from topos.query.types import FORBIDDEN_INFERENCE_PUBLIC_KEYS

pytestmark = pytest.mark.acceptance


def test_at_05c_inference_no_evidence_trail() -> None:
    layer = DefaultGameLayer()
    result = layer.apply(
        context_packet={"scores": [{"value": 0.8}], "snippets": ["secret"], "sources": ["x"]},
        access_mode="inference",
        scope_id="availability:read",
    )
    payload = result.to_dict()
    for key in FORBIDDEN_INFERENCE_PUBLIC_KEYS:
        assert key not in payload
