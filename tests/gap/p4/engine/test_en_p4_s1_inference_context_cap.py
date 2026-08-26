"""
Gap: Inference — unbounded context → cap enforced
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import json

import pytest

from topos.query.inference import DEFAULT_MAX_CONTEXT_CHARS, build_inference_context_packet

pytestmark = pytest.mark.gap


def test_inference_context_truncates_at_cap() -> None:
    huge = {"rows": [{"content": "x" * 5000}]}
    packet = build_inference_context_packet(huge, max_chars=DEFAULT_MAX_CONTEXT_CHARS)
    assert packet["context_truncated"] is True
    assert packet["context"].endswith("[CONTEXT CUT AT CHAR LIMIT]")
    assert len(packet["context"]) <= DEFAULT_MAX_CONTEXT_CHARS


def test_inference_context_under_cap_not_truncated() -> None:
    small = {"scores": [{"value": 0.5}]}
    packet = build_inference_context_packet(small, max_chars=DEFAULT_MAX_CONTEXT_CHARS)
    assert packet["context_truncated"] is False
    assert json.loads(packet["context"]) == small
