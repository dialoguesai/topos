"""Goal clustering: the pruned, vectorized pass returns exactly what the all-pairs
loop returned — same clusters, same roots, same order.

The loop cost 985.7s of every entity-graph rebuild on the owner's node (3,222
goals, 5.19M pairs, 2026-09-11) and pushed the rebuild past its 1800s cap.
``_reference`` below is that loop verbatim, so any drift between the two fails
here rather than as a goal node that silently split or merged.
"""

from __future__ import annotations

import math
import random

import pytest

from topos.features.entities import graph_enrichers as ge
from topos.features.entities.resolver import normalize_name, token_set_similarity
from topos.features.signal.vector_math import cosine_similarity


def _reference(grouped, embed_fn):
    keys = list(grouped.keys())
    if len(keys) <= 1:
        return {k: [k] for k in keys}
    vectors = None
    raw = embed_fn([grouped[k]["text"] for k in keys])
    if raw and len(raw) == len(keys):
        vectors = raw
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    cos = cosine_similarity if vectors is not None else None
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            similar = False
            if cos is not None:
                try:
                    similar = cos(vectors[i], vectors[j]) >= ge.GOAL_EMBED_MERGE_SCORE
                except Exception:
                    similar = False
            if not similar:
                similar = token_set_similarity(keys[i], keys[j]) >= ge.GOAL_TOKEN_MERGE_SCORE
            if similar:
                union(keys[i], keys[j])
    clusters = {}
    for k in keys:
        clusters.setdefault(find(k), []).append(k)
    return clusters


def _grouped(texts):
    grouped = {}
    for text in texts:
        grouped.setdefault(normalize_name(text), {"text": text, "goal_id": text, "events": [], "records": []})
    return grouped


def _assert_same(texts, embed_fn):
    grouped = _grouped(texts)
    expected = _reference(grouped, embed_fn)
    actual = ge._cluster_goal_keys(grouped, embed_fn)
    assert list(actual.items()) == list(expected.items())


# Cases the bounds have to get exactly right: typos that share no token (only
# the character bound can admit them), contained token sets (exactly 1.0), a
# goal that normalizes to nothing (always 0.0), accents, reordering, and
# near-misses either side of 0.8.
CRAFTED = [
    "Organise the garage",
    "Organize the garage",
    "Learn Spanish",
    "Learn Spanish this year",
    "learn spanish",
    "!!!",
    "???",
    "Open the café",
    "Open the cafe",
    "Run a marathon in under four hours",
    "Run a marathon under four hours",
    "Finish the quarterly report",
    "Finish quarterly reports",
    "Call Mom every Sunday",
    "Call Dad every Sunday",
    "Deepen Orion scope coverage",
    "Deepen the Orion scope coverage work",
    "Save money",
    "Save more money for the house",
    "Read 20 books",
    "Read twenty books",
    "Dr. Placeholder check-in",
    "Placeholder check-in",
    "Ship v2",
    "Ship v2.1",
    "a",
    "b",
]


def _unit(rng, dim):
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def _at_cosine(u, w, c):
    """A vector at cosine ``c`` from ``u`` (u, w unit and near-orthogonal)."""
    dot = sum(a * b for a, b in zip(u, w))
    w_perp = [b - dot * a for a, b in zip(u, w)]
    norm = math.sqrt(sum(x * x for x in w_perp))
    w_perp = [x / norm for x in w_perp]
    s = math.sqrt(1.0 - c * c)
    return [c * a + s * b for a, b in zip(u, w_perp)]


def test_tokens_only_matches_the_all_pairs_loop():
    _assert_same(CRAFTED, lambda texts: None)


def test_with_vectors_matches_including_pairs_on_the_threshold():
    rng = random.Random(7)

    def embed(texts):
        vecs = [_unit(rng, 16) for _ in texts]
        # Planted pairs just under, at, and just over GOAL_EMBED_MERGE_SCORE.
        t = ge.GOAL_EMBED_MERGE_SCORE
        # CRAFTED normalizes to fewer keys than it has texts ("Learn Spanish" and
        # "learn spanish" are one key), so the pairs are placed within range.
        last = len(vecs) - 1
        for (i, j), c in zip([(2, 11), (6, 13), (15, 20), (0, last)], [t - 1e-7, t, t + 1e-7, t - 1e-12]):
            vecs[j] = _at_cosine(vecs[i], vecs[j], c)
        vecs[5] = [0.0] * 16  # a zero vector scores 0.0 against everything
        return vecs

    grouped = _grouped(CRAFTED)
    frozen = embed([g["text"] for g in grouped.values()])
    _assert_same(CRAFTED, lambda texts: frozen)


def test_vectors_it_cannot_batch_follow_the_loops_rule():
    grouped = _grouped(CRAFTED)
    n = len(grouped)
    ragged = [[1.0, 0.0, 0.0] if i % 2 else [1.0, 0.0] for i in range(n)]
    _assert_same(CRAFTED, lambda texts: ragged)
    broken = [[1.0, 0.0] for _ in range(n)]
    broken[3] = ["x", "y"]  # cosine_similarity raises on this one; the loop treats that as dissimilar
    _assert_same(CRAFTED, lambda texts: broken)


@pytest.mark.parametrize("seed", range(40))
def test_random_goal_sets_match_the_all_pairs_loop(seed):
    rng = random.Random(seed)
    words = [
        "build", "the", "garage", "garden", "gardening", "learn", "spanish", "french",
        "save", "money", "house", "run", "marathon", "read", "books", "book", "call",
        "mom", "dad", "report", "reports", "quarterly", "ship", "launch", "orion",
        "scope", "coverage", "a", "to", "and", "cafe", "café", "organise", "organize",
    ]
    texts = [
        " ".join(rng.choice(words) for _ in range(rng.randint(1, 6)))
        + ("" if rng.random() < 0.8 else rng.choice(["!", "'s", " s", "."]))
        for _ in range(rng.randint(20, 120))
    ]
    grouped = _grouped(texts)
    use_vectors = seed % 2 == 0
    if use_vectors:
        base = [_unit(rng, 8) for _ in range(6)]
        vecs = []
        for _ in grouped:
            b = rng.choice(base)
            noise = _unit(rng, 8)
            mix = [x + rng.uniform(0.0, 0.6) * y for x, y in zip(b, noise)]
            vecs.append(mix)
        frozen = vecs
        _assert_same(texts, lambda _texts: frozen)
    else:
        _assert_same(texts, lambda _texts: None)
