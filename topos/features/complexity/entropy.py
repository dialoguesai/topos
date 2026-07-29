"""Shannon entropy and distribution helpers."""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

import numpy as np
from scipy.stats import entropy as scipy_entropy


def shannon_entropy_bits(counts: Mapping[str, int] | Sequence[float], *, alpha: float = 1.0) -> float:
    """Dirichlet-smoothed Shannon entropy in bits."""
    if isinstance(counts, Mapping):
        values = [float(v) for v in counts.values()]
    else:
        values = [float(v) for v in counts]
    if not values:
        return 0.0
    total = sum(values) + alpha * len(values)
    if total <= 0:
        return 0.0
    probs = [(v + alpha) / total for v in values]
    probs_arr = np.asarray(probs, dtype=float)
    probs_arr = probs_arr[probs_arr > 0]
    if len(probs_arr) == 0:
        return 0.0
    return float(scipy_entropy(probs_arr, base=2))


def js_divergence_bits(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """Jensen–Shannon divergence in bits."""
    keys = set(p.keys()) | set(q.keys())
    if not keys:
        return 0.0
    p_vec = np.array([float(p.get(k, 0.0)) for k in keys], dtype=float)
    q_vec = np.array([float(q.get(k, 0.0)) for k in keys], dtype=float)
    p_sum, q_sum = p_vec.sum(), q_vec.sum()
    if p_sum <= 0 or q_sum <= 0:
        return 0.0
    p_vec /= p_sum
    q_vec /= q_sum
    m_vec = 0.5 * (p_vec + q_vec)
    kl_pm = float(scipy_entropy(p_vec, m_vec, base=2))
    kl_qm = float(scipy_entropy(q_vec, m_vec, base=2))
    return 0.5 * (kl_pm + kl_qm)


def clamp_score(value: float, *, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def entropy_bits(weights: Sequence[float]) -> float:
    """Plain Shannon entropy in bits over already-smooth continuous weights."""
    values = np.asarray([float(v) for v in weights if v > 0], dtype=float)
    if values.size == 0:
        return 0.0
    total = values.sum()
    if total <= 0:
        return 0.0
    return float(scipy_entropy(values / total, base=2))


def normalized_entropy(weights: Sequence[float]) -> float:
    """H / log2(K) in [0, 1]; 0 for K <= 1 (fully concentrated).

    Unlike a fixed max_bits clip, this stays sensitive at any scale: the live
    graph's 15.4 combined bits saturated the v1 formula to a constant 0.0.
    """
    values = [float(v) for v in weights if v > 0]
    k = len(values)
    if k <= 1:
        return 0.0
    return entropy_bits(values) / math.log2(k)


def effective_count(weights: Sequence[float]) -> float:
    """Perplexity 2^H: 'behaves like N equally weighted items'."""
    values = [float(v) for v in weights if v > 0]
    if not values:
        return 0.0
    return float(2.0 ** entropy_bits(values))


def entropy_to_focus_score(entropy_sum: float, *, max_bits: float = 8.0) -> float:
    """Map combined entropy (bits) to 0–100 focus (higher = more concentrated)."""
    if max_bits <= 0:
        return 0.0
    normalized = min(1.0, entropy_sum / max_bits)
    return clamp_score(100.0 * (1.0 - normalized))


def jaccard_distance(a: Iterable[str], b: Iterable[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return 1.0 - (len(set_a & set_b) / len(union))
