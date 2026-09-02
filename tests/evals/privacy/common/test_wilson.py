"""Unit tests for the one-sided Wilson upper bound (§E1: print the bound, never bare 0.0).

The bound is what turns "0 leaks out of N probes" from a gate into a measurement:
0/N only bounds the true rate to ≲3/N at 95% (rule of three), and the scorecard
must say so with the actual N.
"""

from __future__ import annotations

import math

import pytest

from tests.evals.privacy.common.wilson import Z_ONE_SIDED_95, wilson_upper_bound

pytestmark = [pytest.mark.private]


def _score_equation_residual(p: float, successes: int, n: int, z: float) -> float:
    """The Wilson UB is the larger root of (p - p̂)² = z²·p(1-p)/n.

    Independent oracle: plug the closed form back into the defining equation.
    """
    p_hat = successes / n
    return (p - p_hat) ** 2 - (z**2) * p * (1.0 - p) / n


def test_zero_successes_matches_rule_of_three_at_large_n():
    """For 0/N at large N, Wilson UB ≈ z²/N = 2.706/N — the rule-of-three 3/N
    approximation, slightly tighter (rule of three is the conservative cousin)."""
    for n in (1_000, 10_000, 1_000_000):
        ub = wilson_upper_bound(0, n)
        rule_of_three = 3.0 / n
        assert ub < rule_of_three, "one-sided Wilson at 0/N sits below 3/N"
        # Within ~10% of the rule-of-three number (z² / 3 ≈ 0.902).
        assert abs(ub - rule_of_three) / rule_of_three < 0.11
        # Asymptotically N·UB → z².
        assert math.isclose(n * ub, Z_ONE_SIDED_95**2, rel_tol=1e-2)


def test_zero_successes_closed_form():
    """0/N has the closed form z²/(N + z²) — hand-checkable."""
    z2 = Z_ONE_SIDED_95**2
    for n in (1, 6, 15, 34, 270):
        assert math.isclose(wilson_upper_bound(0, n), z2 / (n + z2), rel_tol=1e-12)


def test_hand_computed_cases():
    # n=34 (the plan's "today's n≈34 reads ≤ ~9%").
    assert math.isclose(wilson_upper_bound(0, 34), 0.0737, abs_tol=5e-4)
    # 1 success in 10 trials, one-sided 95%.
    assert math.isclose(wilson_upper_bound(1, 10), 0.3477, abs_tol=5e-4)
    # 0 of 6 (today's UAR battery size): the bound is a loud ~31%, which is the point.
    assert math.isclose(wilson_upper_bound(0, 6), 0.3108, abs_tol=5e-4)


@pytest.mark.parametrize("successes,n", [(0, 5), (0, 100), (1, 10), (3, 34), (7, 50)])
def test_satisfies_defining_score_equation(successes, n):
    ub = wilson_upper_bound(successes, n)
    residual = _score_equation_residual(ub, successes, n, Z_ONE_SIDED_95)
    assert abs(residual) < 1e-12
    assert ub > successes / n  # upper root, strictly above the point estimate


def test_bounds_and_edge_cases():
    assert wilson_upper_bound(0, 0) == 1.0  # no data → no information
    assert wilson_upper_bound(5, 5) == 1.0  # all failures → bound clamps to 1
    assert 0.0 < wilson_upper_bound(0, 1) < 1.0
    for n in (1, 10, 1000):
        assert 0.0 <= wilson_upper_bound(0, n) <= 1.0


def test_monotone_tightens_with_n():
    """More probes → tighter bound. This is the release-over-release story."""
    bounds = [wilson_upper_bound(0, n) for n in (6, 15, 34, 90, 270, 1000)]
    assert bounds == sorted(bounds, reverse=True)


def test_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        wilson_upper_bound(-1, 10)
    with pytest.raises(ValueError):
        wilson_upper_bound(11, 10)
    with pytest.raises(ValueError):
        wilson_upper_bound(0, -1)
