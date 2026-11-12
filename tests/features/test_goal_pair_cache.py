"""Goal clustering keeps its pair decisions between rebuilds (goal_pair_cache) and still makes the full comparison's pairs.

The full comparison grows as n²: 574 s of a 627 s rebuild for 9,848 goals on a copy of the owner's node (4 Nov 2026).
What these stop: a rebuild that scores fewer pairs than it must (a new or edited goal compared with only some of the
others), a cache that keeps a pair after one side left or changed, a cache that outlives a change of rule or embedder,
and a cache that cannot be read failing the rebuild instead of costing it the full comparison.
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from topos.features.entities import goal_pair_cache as gpc
from topos.features.entities import graph_enrichers as ge
from tests.features.test_goal_cluster_equivalence import _grouped, _unit

WORDS = [
    "build", "the", "garage", "garden", "gardening", "learn", "spanish", "french", "save", "money", "house", "run",
    "marathon", "read", "books", "book", "call", "report", "reports", "quarterly", "ship", "launch", "scope",
    "coverage", "a", "to", "and", "cafe", "café", "organise", "organize",
]


def _texts(rng, n):
    return [" ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 5))) for _ in range(n)]


class _Embedder:
    """One vector per text, drawn near one of six directions: the same text always gets the same vector."""

    def __init__(self, rng):
        self.rng = rng
        self.base = [_unit(rng, 8) for _ in range(6)]
        self.seen = {}

    def __call__(self, texts):
        for text in texts:
            if text not in self.seen:
                b, noise = self.rng.choice(self.base), _unit(self.rng, 8)
                self.seen[text] = [x + self.rng.uniform(0.0, 0.5) * y for x, y in zip(b, noise)]
        return [self.seen[t] for t in texts]


def _inputs(grouped, embed):
    keys = list(grouped)
    texts = [grouped[k]["text"] for k in keys]
    return keys, texts, (embed(texts) if embed else None)


@pytest.mark.parametrize("seed", range(20))
def test_rows_scores_exactly_the_pairs_that_touch_them(seed):
    rng = random.Random(seed)
    grouped = _grouped(_texts(rng, rng.randint(10, 80)))
    keys, _, vectors = _inputs(grouped, _Embedder(rng) if seed % 2 == 0 else None)
    if seed % 5 == 4 and vectors is not None:
        vectors = [v if i % 3 else v[:-1] for i, v in enumerate(vectors)]  # ragged: the pair-by-pair path
    full = ge._similar_goal_pairs(keys, vectors)
    rows = set(rng.sample(range(len(keys)), rng.randint(0, len(keys))))
    assert ge._similar_goal_pairs(keys, vectors, rows=rows) == [p for p in full if p[0] in rows or p[1] in rows]


@pytest.mark.parametrize("seed", range(16))
def test_the_cache_makes_the_full_comparisons_pairs_through_additions_removals_and_edits(seed):
    rng = random.Random(seed)
    embed = _Embedder(rng) if seed % 2 == 0 else None
    conn = sqlite3.connect(":memory:")
    cache = gpc.GoalPairCache(conn, "test-embedder")
    texts = _texts(rng, rng.randint(20, 60))
    before = set()
    for step in range(5):
        grouped = _grouped(texts)
        if step == 3:  # an edit: a key keeps its place and changes its text (and so its vector)
            for key in rng.sample(list(grouped), min(4, len(grouped))):
                grouped[key]["text"] = grouped[key]["text"] + " soon"
        keys, group_texts, vectors = _inputs(grouped, embed)
        pairs, counts = gpc.similar_pairs_with_cache(keys, group_texts, vectors, cache)
        assert pairs == ge._similar_goal_pairs(keys, vectors), f"step {step}"
        now = set(zip(keys, group_texts))
        assert counts == {"goal_items": len(keys), "goal_items_scored": len(now - before)}, f"step {step}"
        before = now
        kept = [t for t in texts if rng.random() < 0.85]  # some goals leave, some arrive
        texts = kept + _texts(rng, rng.randint(0, 8))
    conn.close()


def test_the_same_goals_score_nothing_and_a_new_rule_or_embedder_scores_everything(monkeypatch):
    rng = random.Random(3)
    grouped = _grouped(_texts(rng, 40))
    keys, texts, vectors = _inputs(grouped, _Embedder(rng))
    conn = sqlite3.connect(":memory:")
    n = len(keys)

    def scored(tag):
        return gpc.similar_pairs_with_cache(keys, texts, vectors, gpc.GoalPairCache(conn, tag))[1]["goal_items_scored"]

    assert scored("model-a") == n and scored("model-a") == 0
    assert scored("model-b") == n, "vectors from another embedder are not the ones the pairs were decided on"
    monkeypatch.setattr(ge, "GOAL_EMBED_MERGE_SCORE", ge.GOAL_EMBED_MERGE_SCORE - 0.01)
    assert scored("model-b") == n, "a pair decided under another threshold is decided again"
    assert gpc.similar_pairs_with_cache(keys, texts, None, gpc.GoalPairCache(conn, "model-b"))[1]["goal_items_scored"] == n


def test_a_cache_that_cannot_be_read_or_written_costs_the_full_comparison_and_nothing_else():
    rng = random.Random(4)
    grouped = _grouped(_texts(rng, 30))
    keys, texts, vectors = _inputs(grouped, _Embedder(rng))
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {gpc.ITEMS_TABLE} (other TEXT)")  # not the shape the cache reads or writes
    conn.execute(f"CREATE TABLE {gpc.PAIRS_TABLE} (other TEXT)")
    for _ in range(2):
        pairs, counts = gpc.similar_pairs_with_cache(keys, texts, vectors, gpc.GoalPairCache(conn, "t"))
        assert pairs == ge._similar_goal_pairs(keys, vectors) and counts["goal_items_scored"] == len(keys)


def test_clusters_with_a_warm_cache_are_the_clusters_without_one():
    rng = random.Random(9)
    embed = _Embedder(rng)
    conn = sqlite3.connect(":memory:")
    texts = _texts(rng, 70)
    expected = ge._cluster_goal_keys(_grouped(texts), embed)
    for _ in range(2):  # cold, then warm
        mode = {}
        actual = ge._cluster_goal_keys(_grouped(texts), embed, mode, cache=gpc.GoalPairCache(conn, "t"))
        assert list(actual.items()) == list(expected.items())
    assert mode["goal_items_scored"] == "0"


def test_after_a_failed_read_the_cache_is_replaced_so_a_goal_that_comes_back_is_compared_again():
    conn = sqlite3.connect(":memory:")

    def run(texts):
        grouped = _grouped(texts)
        keys, group_texts, _ = _inputs(grouped, None)
        pairs, _ = gpc.similar_pairs_with_cache(keys, group_texts, None, gpc.GoalPairCache(conn, "t"))
        assert pairs == ge._similar_goal_pairs(keys, None)
        return [(keys[i], keys[j]) for i, j in pairs]

    run(["learn spanish", "save money"])
    conn.execute(f"ALTER TABLE {gpc.PAIRS_TABLE} RENAME TO unreadable")  # this rebuild cannot read its pairs
    run(["save money", "learn spanish this year"])
    # A difference against that empty read would have kept "learn spanish" as compared, and skipped this pair.
    assert ("learn spanish", "learn spanish this year") in run(["learn spanish", "learn spanish this year", "save money"])
