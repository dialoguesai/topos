"""One-sided Wilson upper bound for 0/N-style privacy rates (§E1).

A scorecard that prints "0.0" for UAR/CER is a *gate*, not a *measurement*:
observing 0 leaks in N probes only bounds the true leak rate to about 3/N at
95% confidence (the rule of three). The Wilson score interval gives the exact
one-sided bound from the actual probe count, so every rate is reported as
"0.0 of N (95% UB X.X%)" and the bound tightens as the probe corpus grows.

Pure function, no dependencies — safe for the deterministic release lane.
"""

from __future__ import annotations

import math

# One-sided 95% normal quantile (Φ⁻¹(0.95)). The scorecard's bound is one-sided
# because the claim being defended is "the leak rate is not above X".
Z_ONE_SIDED_95 = 1.6448536269514722


def wilson_upper_bound(successes: int, n: int, *, z: float = Z_ONE_SIDED_95) -> float:
    """Upper limit of the one-sided Wilson score interval for a binomial rate.

    `successes` is the number of observed events (leaks) out of `n` trials
    (probes). Returns the largest plausible true rate at the confidence encoded
    by `z` (default: one-sided 95%).

    For successes == 0 this reduces to z²/(n + z²) ≈ 2.71/n at large n — the
    exact form of the rule-of-three approximation 3/n. n == 0 returns 1.0:
    no probes were run, so nothing is bounded.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if successes < 0 or successes > n:
        raise ValueError(f"successes must be within [0, n={n}], got {successes}")
    if n == 0 or successes == n:
        return 1.0
    p_hat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    spread = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    return min(1.0, (center + spread) / denom)
