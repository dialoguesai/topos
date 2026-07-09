"""D3 regression: goal-intent queries must surface the owner's authored goals.

Two mechanisms, both needed:
  * the goal lane reads from the scope's authorized default_source_ids, not the
    runtime-install subset (goals live under a bundled source that lacks an
    install row);
  * the fusion per-lane floor (min_per_source) guarantees the single-lane goal
    items are not crowded out of the capped result by high-volume lanes.
"""

from __future__ import annotations

from topos.query.retrieval import _rrf_fuse_summary_lists


def _items(source, n, prefix):
    return [
        {"summary_text": f"{prefix}-{i}", "record_id": f"{prefix}-{i}",
         "retrieval_source": source}
        for i in range(n)
    ]


def test_min_per_source_guarantees_single_lane_representation():
    # A heavier high-volume lane fills every cap slot and buries the few
    # single-lane goal items (in the real pipeline the weight advantage comes
    # from multi-lane score accumulation + recency); the floor promotes them.
    vector = _items("vector", 30, "v")
    goals = _items("user_goal", 4, "g")
    fused = _rrf_fuse_summary_lists(
        [("vector", 3.0, vector), ("goals", 1.0, goals)],
        cap=25,
        min_per_source={"goals": 2},
    )
    sources = [i["retrieval_source"] for i in fused]
    assert sources.count("user_goal") >= 2  # guaranteed floor honored
    assert len(fused) == 25  # cap preserved, floor displaced low-ranked vector


def test_min_per_source_none_leaves_fusion_untouched():
    vector = _items("vector", 30, "v")
    goals = _items("user_goal", 4, "g")
    baseline = _rrf_fuse_summary_lists(
        [("vector", 3.0, vector), ("goals", 1.0, goals)], cap=25
    )
    # Without the floor the goals (single lane, out-weighted) get squeezed out.
    assert all(i["retrieval_source"] == "vector" for i in baseline)


def test_min_per_source_no_op_when_lane_empty():
    vector = _items("vector", 30, "v")
    fused = _rrf_fuse_summary_lists(
        [("vector", 1.0, vector), ("goals", 1.0, [])],
        cap=25,
        min_per_source={"goals": 2},
    )
    assert len(fused) == 25
    assert all(i["retrieval_source"] == "vector" for i in fused)
