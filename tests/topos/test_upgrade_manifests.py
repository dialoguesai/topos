"""Upgrade manifests: the reviewed 'what does this release invalidate' ledger.

Guards the shape (a malformed entry must fail CI, not the fleet) and the
version-range union semantics the startup re-derivation job will rely on.
"""

from __future__ import annotations

import pytest

from topos.upgrades import (
    STEP_KINDS,
    UNRELEASED_VERSION,
    load_manifests,
    load_unreleased,
    steps_between,
)

pytestmark = pytest.mark.public


def test_manifests_load_and_validate():
    releases = load_manifests()
    assert releases, "manifest registry must not be empty"
    versions = [r["version"] for r in releases]
    assert UNRELEASED_VERSION not in versions
    assert versions == sorted(versions, key=lambda v: tuple(map(int, v.split(".")))), (
        "releases must be strictly ordered by version"
    )
    for release in releases:
        for step in release.get("steps", []):
            assert step["kind"] in STEP_KINDS
            assert step["id"] and step["why"]


def test_unreleased_staging_is_excluded_from_steps_between():
    staging = load_unreleased()
    assert staging is not None
    assert staging["version"] == UNRELEASED_VERSION
    # Even if unreleased has steps, steps_between must never see them.
    shipped = load_manifests()
    latest = shipped[-1]["version"]
    ids = {s["id"] for s in steps_between("1.0.0", latest)}
    for step in staging.get("steps") or []:
        if step["kind"] == "none":
            continue
        assert step["id"] not in ids


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
