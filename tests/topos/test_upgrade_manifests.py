"""Upgrade manifests: the reviewed 'what does this release invalidate' ledger.

Guards the shape (a malformed entry must fail CI, not the fleet) and the
version-range union semantics the startup re-derivation job will rely on.
"""

from __future__ import annotations

import pytest

from topos.upgrades import STEP_KINDS, load_manifests, steps_between

pytestmark = pytest.mark.public


def test_manifests_load_and_validate():
    releases = load_manifests()
    assert releases, "manifest registry must not be empty"
    versions = [r["version"] for r in releases]
    assert versions == sorted(versions, key=lambda v: tuple(map(int, v.split(".")))), (
        "releases must be strictly ordered by version"
    )
    for release in releases:
        for step in release.get("steps", []):
            assert step["kind"] in STEP_KINDS
            assert step["id"] and step["why"]


def test_1_2_0_declares_the_stitcher_reextraction_and_graph_rebuild():
    steps = steps_between("1.1.0", "1.2.0")
    ids = [s["id"] for s in steps]
    assert "reextract-entities" in ids
    assert "rebuild-entity-graph" in ids
    # The rebuild must come after re-extraction (partial mentions gut the graph).
    assert ids.index("reextract-entities") < ids.index("rebuild-entity-graph")
    rebuild = next(s for s in steps if s["id"] == "rebuild-entity-graph")
    assert "reextract-entities" in (rebuild.get("depends_on") or [])


def test_same_version_upgrade_is_a_noop():
    assert steps_between("1.2.0", "1.2.0") == []


def test_documentation_only_steps_are_not_executed():
    steps = steps_between("1.1.0", "1.2.0")
    assert all(s["kind"] != "none" for s in steps)


def test_multi_hop_unions_and_dedupes():
    # From below the oldest manifest: every executable step in range applies once.
    steps = steps_between("1.0.0", "1.2.0")
    ids = [s["id"] for s in steps]
    assert len(ids) == len(set(ids)), "duplicate step ids must dedupe"
    assert "reextract-entities" in ids and "rebuild-entity-graph" in ids
