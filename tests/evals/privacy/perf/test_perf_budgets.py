"""§F.6 — latency budgets + per-stage waterfall.

Tier-1 hard gate: the deny path p95 (fast, deterministic; a slow deny is a side-channel).
The rest is reported for trend + waterfall attribution with generous sanity budgets — a
portable CI can't set a production p95 dominated by hardware and model cold-start.
"""

from __future__ import annotations

import pytest

from tests.evals.privacy.perf.perf_harness import (
    build_perf_report,
    deny_call,
    grantee_summary_call,
    negotiation_resolution_wall_clock,
    percentile,
    stage_waterfall,
    time_calls,
)

pytestmark = [pytest.mark.private]

DENY_P95_BUDGET_MS = 500.0  # the plan's existing deny budget; a slow deny is a side-channel
GRANTEE_SUMMARY_P95_SANITY_MS = 5000.0  # generous — catches gross regressions, not hardware jitter


def test_deny_path_p95_within_budget():
    """Tier-1 gate: deny must be fast."""
    lat = time_calls(deny_call, n=15)
    p95 = percentile(lat, 95)
    assert p95 <= DENY_P95_BUDGET_MS, f"deny p95 {p95:.1f}ms exceeds {DENY_P95_BUDGET_MS}ms"


def test_grantee_summary_p95_sanity():
    lat = time_calls(grantee_summary_call, n=12)
    p95 = percentile(lat, 95)
    assert p95 <= GRANTEE_SUMMARY_P95_SANITY_MS, f"grantee-summary p95 {p95:.1f}ms is a gross regression"


def test_deny_is_faster_than_summary():
    """Structural: a deny short-circuits before retrieval, so it must beat a real query."""
    deny_p95 = percentile(time_calls(deny_call, n=12), 95)
    summary_p95 = percentile(time_calls(grantee_summary_call, n=12), 95)
    assert deny_p95 < summary_p95


def test_stage_waterfall_attributes_every_stage():
    timings = stage_waterfall(minimizer=True)
    for key in ("retrieval_ms", "deterministic_filter_ms", "minimizer_ms", "game_layer_ms", "total_ms"):
        assert key in timings, f"missing stage timing: {key}"
    # total is at least the sum of the parts is not required (overlap/overhead), but positive.
    assert timings["total_ms"] > 0
    assert timings["minimizer_ms"] >= 0  # minimizer line feeds D's gain-per-ms


def test_negotiation_wall_clock_reported():
    neg = negotiation_resolution_wall_clock()
    assert neg["rounds"] >= 2, "arm C should negotiate at least one round"
    assert neg["full_resolution_ms"] > 0
    assert neg["per_round_ms"] > 0


def test_perf_report_shape():
    report = build_perf_report(n=6)
    assert set(report["paths"]) == {"deny", "grantee_summary", "owner_raw"}
    for path, pv in report["paths"].items():
        assert pv["p95"] >= pv["p50"] >= 0, path
    assert report["stage_waterfall_ms"]["total_ms"] > 0
