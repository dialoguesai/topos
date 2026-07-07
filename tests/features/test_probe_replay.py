"""Probe replay comparison logic (drift measurement plumbing)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "probe_replay", REPO_ROOT / "scripts" / "probe_replay.py"
)
probe_replay = importlib.util.module_from_spec(spec)
sys.modules["probe_replay"] = probe_replay
spec.loader.exec_module(probe_replay)


def _run(ts: str, ids_by_probe: dict) -> dict:
    return {
        "ts": ts,
        "probe_set_version": probe_replay.PROBE_SET_VERSION,
        "probes": [{"probe": p, "top_ids": ids} for p, ids in ids_by_probe.items()],
    }


def test_identical_runs_have_zero_churn():
    prev = _run("t0", {"a": ["1", "2", "3"], "b": ["4", "5"]})
    curr = _run("t1", {"a": ["1", "2", "3"], "b": ["4", "5"]})
    cmp = probe_replay.compare_with_previous(curr, prev)
    assert cmp["rank_churn"] == 0.0
    assert cmp["compared_to"] == "t0"


def test_disjoint_runs_have_full_churn():
    prev = _run("t0", {"a": ["1", "2"]})
    curr = _run("t1", {"a": ["8", "9"]})
    cmp = probe_replay.compare_with_previous(curr, prev)
    assert cmp["rank_churn"] == 1.0


def test_no_previous_run_is_null_comparison():
    curr = _run("t1", {"a": ["1"]})
    assert probe_replay.compare_with_previous(curr, None) == {
        "rank_churn": None,
        "compared_to": None,
    }


def test_probe_set_version_mismatch_skips_comparison():
    prev = _run("t0", {"a": ["1"]})
    prev["probe_set_version"] = 0
    curr = _run("t1", {"a": ["1"]})
    cmp = probe_replay.compare_with_previous(curr, prev)
    assert cmp["rank_churn"] is None


def test_per_probe_overlap_annotated():
    prev = _run("t0", {"a": ["1", "2", "3", "4"]})
    curr = _run("t1", {"a": ["1", "2", "5", "6"]})
    probe_replay.compare_with_previous(curr, prev)
    assert curr["probes"][0]["top10_jaccard_vs_prev"] == 0.333
