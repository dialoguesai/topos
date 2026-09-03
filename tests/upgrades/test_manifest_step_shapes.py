"""Every manifest step must be readable by the executor it names.

protects: an upgrade step's parameters land where its executor actually
reads them — a step whose targets sit one level off is valid JSON, runs, and
does something else entirely.

Live 2026-09-03: the 1.3.47 step ``index-goals-into-derived-index`` declared
``targets: ["derived_object_index"]`` at the TOP level of the step, but
``_exec_derived_rebuild`` reads ``step["params"]["targets"]``. The list was
therefore invisible, the executor fell back to its default
(``["entity_graph"]``), and the step ran a graph rebuild — which lost a race
for the rebuild lock and recorded ``failed`` while the goal index it was
supposed to build stayed empty. Nothing was malformed; it simply asked for
the wrong work, silently.

This is the same file and the same class of hazard as the 2026-08-18
manifest edit that appended prose INTO ``steps[0].params.targets``: JSON
that parses is not JSON that means anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFESTS = Path(__file__).resolve().parents[2] / "topos" / "upgrades" / "manifests.json"

#: Targets `_exec_derived_rebuild` recognizes (runner.py). A target outside
#: this set falls through its if/elif chain and is silently not run.
_DERIVED_REBUILD_TARGETS = {
    "entity_graph", "graph", "entities_graph",
    "topic_clusters", "clusters",
    "topic_cluster_labels", "cluster_labels",
    "blackhole_rebuilds", "blackhole",
    "derived_object_index", "derived_objects",
    "closeness_fact_anchors", "closeness_anchors",
    "timeline",
}


def _steps():
    doc = json.loads(MANIFESTS.read_text(encoding="utf-8"))
    for release in doc["releases"]:
        for step in release.get("steps") or []:
            yield str(release.get("version")), step


def test_every_step_kind_has_an_executor():
    from topos.upgrades.runner import DEFAULT_EXECUTORS as STEP_EXECUTORS

    unknown = [
        (v, s.get("id"), s.get("kind"))
        for v, s in _steps()
        if str(s.get("kind")) not in STEP_EXECUTORS
    ]
    assert not unknown, f"steps naming a kind with no executor: {unknown}"


def test_derived_rebuild_targets_are_where_the_executor_reads_them():
    """The 1.3.47 defect, pinned. A `derived_rebuild` step that carries its
    targets outside `params` asks for `entity_graph` no matter what it says."""
    misplaced = []
    for version, step in _steps():
        if str(step.get("kind")) != "derived_rebuild":
            continue
        params = step.get("params") or {}
        in_params = list(params.get("targets") or params.get("layers") or [])
        top_level = list(step.get("targets") or [])
        if top_level and not in_params:
            misplaced.append((version, step.get("id"), top_level))
    assert not misplaced, (
        "derived_rebuild steps whose targets sit outside params (the executor "
        f"reads params.targets and would default to entity_graph): {misplaced}"
    )


def test_declared_derived_rebuild_targets_are_recognized():
    unknown = []
    for version, step in _steps():
        if str(step.get("kind")) != "derived_rebuild":
            continue
        params = step.get("params") or {}
        for target in list(params.get("targets") or params.get("layers") or []):
            if str(target) not in _DERIVED_REBUILD_TARGETS:
                unknown.append((version, step.get("id"), target))
    assert not unknown, (
        f"targets the executor's dispatch does not recognize (silently skipped): {unknown}"
    )


def test_unreleased_entry_stays_a_staging_entry():
    doc = json.loads(MANIFESTS.read_text(encoding="utf-8"))
    unreleased = [r for r in doc["releases"] if r["version"] == "unreleased"]
    assert len(unreleased) == 1, "exactly one staging entry must exist"
