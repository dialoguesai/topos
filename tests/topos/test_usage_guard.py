from __future__ import annotations

import pytest

from topos.engine.usage_guard import classify_guard_submission_error, parse_guard_denial_payload


def test_parse_guard_denial_payload_metric_agnostic():
    detail = {
        "message": "Usage limit reached for this metric in the current period.",
        "reason_code": "LIMIT_EXCEEDED",
        "metric_key": "brand_new_metric",
        "used": 12,
        "limit": 10,
        "unlimited": False,
        "period_start": "2026-01-01T00:00:00+00:00",
        "period_end": "2026-02-01T00:00:00+00:00",
        "policy_version": "policy_v2",
    }
    denial = parse_guard_denial_payload(detail)
    assert denial is not None
    assert denial["reason_code"] == "LIMIT_EXCEEDED"
    assert denial["metric_key"] == "brand_new_metric"
    assert denial["period_start"] == "2026-01-01T00:00:00+00:00"
    assert denial["period_end"] == "2026-02-01T00:00:00+00:00"


def test_classify_guard_submission_error_distinguishes_denial_vs_transport():
    denial_detail = {
        "reason_code": "UNTRUSTED_USAGE_SOURCE",
        "metric_key": "llm_tokens",
        "period_start": "2026-01-01T00:00:00+00:00",
        "period_end": "2026-02-01T00:00:00+00:00",
    }
    assert classify_guard_submission_error(402, denial_detail) == "guard_denial"
    assert classify_guard_submission_error(503, {"error": "timeout"}) == "transport_or_system_error"
