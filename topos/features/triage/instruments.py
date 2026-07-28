"""Formal instruments for attention triage (PLAN_ATTENTION_TRIAGE.md §10).

No LLM anywhere: Tier-0 compression novelty (zlib preset dictionary), Bayesian
surprise (KL on the decayed interest distribution, with per-element
contributions), kNN embedding surprisal, and PageRank attachment. All
deterministic given the same data, hence gate-eligible (D8).
Backtest-validated 2026-07-27 (attention-triage-lab): tier correlation
compression-vs-kNN r=0.779.
"""

from __future__ import annotations

import math
import sqlite3
import zlib
from collections import defaultdict
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

HALF_LIFE_DAYS = 14.0
KNN_K = 8
PAGERANK_DAMPING = 0.85


def decayed_counts(vocab_by_day: Iterable[Tuple[str, List[str]]], asof: date,
                   half_life_days: float = HALF_LIFE_DAYS) -> Dict[str, float]:
    """Exponentially decayed vocab counts: (day_iso, vocab_tokens) pairs → weights."""
    counts: Dict[str, float] = defaultdict(float)
    for day_iso, vocab in vocab_by_day:
        age = (asof - date.fromisoformat(day_iso[:10])).days
        if age < 0:
            continue
        w = 0.5 ** (age / half_life_days)
        for v in vocab:
            counts[v] += w
    return dict(counts)


def to_dist(counts: Dict[str, float], support: set, alpha: float = 0.5) -> Dict[str, float]:
    n = sum(counts.values())
    a = alpha / max(len(support), 1)
    tot = n + alpha
    return {v: (counts.get(v, 0.0) + a) / tot for v in support}


def kl_divergence(post: Dict[str, float], prior: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    kl, contrib = 0.0, {}
    for v, p in post.items():
        q = prior.get(v, 1e-12)
        c = p * math.log(p / q)
        kl += c
        contrib[v] = c
    return kl, contrib


def bayes_surprise(prior_counts: Dict[str, float], added_vocab: Iterable[List[str]]):
    """KL(pi' || pi) after assimilating the added items' vocab; plus per-element movers."""
    post = dict(prior_counts)
    for vocab in added_vocab:
        for v in vocab:
            post[v] = post.get(v, 0.0) + 1.0
    support = set(prior_counts) | set(post)
    if not support:
        return 0.0, {}
    return kl_divergence(to_dist(post, support), to_dist(prior_counts, support))


def comp_novelty(text: str, dict_bytes: bytes) -> Optional[float]:
    """Tier-0: compressed size with the user dictionary / without. >~1 = the
    user's accumulated structure doesn't help = novel. None below 40 bytes."""
    data = text.encode("utf-8", "ignore")
    if len(data) < 40:
        return None

    def size(zdict: Optional[bytes]) -> int:
        co = (zlib.compressobj(9, zlib.DEFLATED, -15, 9, 0, zdict)
              if zdict else zlib.compressobj(9, zlib.DEFLATED, -15))
        return len(co.compress(data) + co.flush())

    return size(dict_bytes[-32768:] if dict_bytes else None) / max(size(None), 1)


def knn_surprisal(vec: np.ndarray, prior_matrix: np.ndarray, k: int = KNN_K) -> Optional[float]:
    """Tier-1: 1 - mean cosine similarity to the k nearest prior embeddings."""
    if prior_matrix.shape[0] < k:
        return None
    sims = prior_matrix @ vec
    return float(1.0 - np.sort(sims)[-k:].mean())


def entity_pagerank(conn: sqlite3.Connection) -> Dict[str, float]:
    """Global PageRank over entity_edges — the attachment-potential substrate."""
    edges = conn.execute(
        "SELECT src_entity_id s, dst_entity_id d, COALESCE(weight, 1.0) w FROM entity_edges"
    ).fetchall()
    nodes = sorted({e[0] for e in edges} | {e[1] for e in edges})
    if not nodes:
        return {}
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    W = np.zeros((n, n))
    for s, d, w in edges:
        W[idx[s], idx[d]] += w
        W[idx[d], idx[s]] += w
    out = W.sum(axis=1, keepdims=True)
    out[out == 0] = 1.0
    P = W / out
    pr = np.full(n, 1.0 / n)
    for _ in range(80):
        pr = (1 - PAGERANK_DAMPING) / n + PAGERANK_DAMPING * (P.T @ pr)
    return {node: float(pr[i]) for node, i in idx.items()}


def zscorer(values: Iterable[Optional[float]]):
    arr = np.array([v for v in values if v is not None], dtype=float)
    if arr.size < 3 or arr.std() == 0:
        return lambda v: 0.0
    mu, sd = arr.mean(), arr.std()
    return lambda v: 0.0 if v is None else float((v - mu) / sd)


# ------------------------------------------------------------- calibration (WS1)

def permutation_null(prior_counts: Dict[str, float], pool_vocabs: List[List[str]],
                     n_items: int, n_perms: int = 1000, seed: int = 0):
    """Null distribution for day-level Bayesian surprise (WS1.1, D12: the pool
    is the trailing-lookback items — the same world the model saw).

    Draws `n_items` item-vocabs from the pool per permutation, computes the
    KL a day of that composition would produce, and additionally tracks the
    max per-token contribution per permutation (family-wise envelope for
    mover significance). Deterministic given `seed`.

    Returns (null_kls: np.ndarray, token_envelope_p95: float).
    """
    if not pool_vocabs or n_items <= 0:
        return np.zeros(0), 0.0
    rng = np.random.default_rng(seed)
    null_kls = np.zeros(n_perms)
    max_contribs = np.zeros(n_perms)
    pool_n = len(pool_vocabs)
    for p in range(n_perms):
        idx = rng.integers(0, pool_n, size=min(n_items, pool_n))
        kl, contrib = bayes_surprise(prior_counts, [pool_vocabs[i] for i in idx])
        null_kls[p] = kl
        max_contribs[p] = max(contrib.values(), default=0.0)
    return null_kls, float(np.quantile(max_contribs, 0.95))


def surprise_percentile(observed_kl: float, null_kls: np.ndarray) -> Optional[float]:
    if null_kls.size == 0:
        return None
    return float((null_kls < observed_kl).mean())


def fit_half_life(day_vocabs: List[Tuple[str, List[List[str]]]],
                  candidates=(3.5, 7.0, 14.0, 28.0), alpha: float = 0.5):
    """Fit the decay half-life by next-day predictive likelihood — the
    deposit-rent criterion used as an estimator (WS1.3).

    day_vocabs: chronological [(day_iso, [item_vocab, ...]), ...]. For each
    candidate h, each day's tokens are scored under the smoothed distribution
    built from all PRIOR days decayed at h; total NLL decides. Returns
    (best_half_life, {h: nll}).
    """
    from datetime import date as _date
    nlls: Dict[float, float] = {}
    for h in candidates:
        total = 0.0
        for t in range(1, len(day_vocabs)):
            day_iso, vocabs = day_vocabs[t]
            asof = _date.fromisoformat(day_iso[:10])
            counts = decayed_counts(
                ((d, v) for d, vs in day_vocabs[:t] for v in vs), asof, half_life_days=h)
            support = set(counts)
            for vocab in vocabs:
                support.update(vocab)
            if not support:
                continue
            dist = to_dist(counts, support, alpha=alpha)
            for vocab in vocabs:
                for tok in vocab:
                    total -= math.log(dist.get(tok, 1e-12))
        nlls[h] = total
    best = min(nlls, key=nlls.get) if nlls else 14.0
    return best, nlls


def length_residualizer(pairs: List[Tuple[int, float]]):
    """OLS of comp-novelty on log length (WS1.4); returns residual(length, comp).
    With too few points, returns the raw value unchanged (residual = value - mean)."""
    data = [(l, c) for l, c in pairs if c is not None and l >= 40]
    if len(data) < 12:
        mu = float(np.mean([c for _, c in data])) if data else 0.0
        return lambda length, comp: None if comp is None else comp - mu
    x = np.log(np.array([l for l, _ in data], dtype=float))
    y = np.array([c for _, c in data])
    slope, intercept = np.polyfit(x, y, 1)
    return (lambda length, comp: None if comp is None
            else float(comp - (slope * math.log(max(length, 1)) + intercept)))


_BLOB_MARKERS = ("$class", "NSObject", "NSKeyedArchiver", "NSValue", "bplist")


def is_blob(text: str) -> bool:
    """BT9: serialized-object junk that must abstain, never reach a quadrant."""
    if not text:
        return False
    if any(m in text for m in _BLOB_MARKERS):
        return True
    sample = text[:400]
    weird = sum(1 for ch in sample if not (ch.isalnum() or ch.isspace() or ch in ".,;:'\"!?()-–—/@&+%$#"))
    return len(sample) >= 40 and weird / len(sample) > 0.30
