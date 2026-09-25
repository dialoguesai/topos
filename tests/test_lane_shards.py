"""CI runs the public lane as slices; together they must still be the whole lane.

ci.yml's job matrix runs one job per slice of tests/lane_shards.py. Ways that
silently stop testing something, none of which turns anything red on its own:
- a slice defined in the table but missing from the matrix: its tests never
  run anywhere;
- a matrix `exclude:`/`include:`, a `continue-on-error`, or an `if:` on the lane
  step: a slice is skipped, or its red is forgiven;
- the packaging job skipped behind a red lane: GitHub counts "skipped" as
  passing for a required check;
- a prefix that no longer names a real path after a rename: its tests fall to
  `rest`, which still runs them, but the balance the table was measured for is
  gone and nobody is told.
This file fails on each. It runs in every slice (lane_shards.EVERY_SLICE), so no
one slice's removal takes it along. It also pins the ownership rule that makes
the slices a partition.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import lane_shards

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _ci_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _ci_matrix_shards() -> list[str]:
    """The `shard: [...]` list in ci.yml, textually, like its sibling guard
    tests/test_local_gate_composition.py reads the `run:` lines."""
    found = re.findall(r"^\s*shard:\s*\[([^\]]*)\]\s*$", _ci_text(), re.MULTILINE)
    assert len(found) == 1, f"expected one `shard: [...]` matrix in {CI_WORKFLOW}, found {found}"
    return [name.strip() for name in found[0].split(",") if name.strip()]


def test_the_ci_matrix_names_every_slice_exactly_once() -> None:
    names = _ci_matrix_shards()
    assert sorted(names) == sorted(lane_shards.SHARDS), (
        f"ci.yml's matrix {names} and tests/lane_shards.py's slices "
        f"{sorted(lane_shards.SHARDS)} disagree. A slice missing from the matrix "
        "is a slice of the lane that CI never runs."
    )


def test_the_ci_lane_step_runs_the_matrix_slice() -> None:
    """Without the flag every job would run the whole lane: nothing lost, four times the minutes."""
    lane_lines = [
        line for line in _ci_text().splitlines()
        if re.match(r"^\s*run:\s*uv run pytest tests\s", line)
    ]
    assert lane_lines, f"no `run: uv run pytest tests ...` line in {CI_WORKFLOW}"
    assert all(f"{lane_shards.OPTION} ${{{{ matrix.shard }}}}" in line for line in lane_lines), lane_lines


def _ci_job(name: str) -> str:
    """One job of ci.yml, textually: from its key to the next job key."""
    match = re.search(
        rf"^  {re.escape(name)}:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:\s*$|\Z)",
        _ci_text(), re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"no `{name}` job in {CI_WORKFLOW}"
    return match.group(1)


def _ci_step(job: str, step_name: str) -> str:
    match = re.search(
        rf"- name: {re.escape(step_name)}\s*\n(.*?)(?=^\s*- name:|\Z)", job, re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"no `{step_name}` step"
    return match.group(1)


def test_nothing_in_the_lane_job_skips_a_slice_or_forgives_its_red() -> None:
    job = _ci_job("public-lane")
    for word in ("exclude:", "include:", "continue-on-error"):
        assert word not in job, (
            f"ci.yml's public-lane job now says `{word}`. That can skip a slice or turn a red "
            "slice green while the matrix list still names it; the slices are the lane."
        )
    lane_step = _ci_step(job, "Run public test lane")
    assert not re.search(r"^\s*if:", lane_step, re.MULTILINE), (
        "the lane step has an `if:`, so some slice can skip its tests and still pass"
    )


def test_the_package_job_goes_red_with_the_lane() -> None:
    """Not merely skipped: a skipped job reports as passing to a required check."""
    job = _ci_job("test-and-package")
    assert re.search(r"^\s*needs:\s*public-lane\s*$", job, re.MULTILINE), job
    assert re.search(r"^    if:\s*\$\{\{\s*!cancelled\(\)\s*\}\}\s*$", job, re.MULTILINE), (
        "test-and-package must run when a slice is red (and only skip on cancel), so that its "
        "check goes red with the lane instead of reporting `skipped`"
    )
    first_step = re.search(r"^\s*steps:\s*\n\s*- name: ([^\n]+)\n(.*?)(?=^\s*- name:)", job,
                           re.MULTILINE | re.DOTALL)
    assert first_step is not None, job
    body = first_step.group(2)
    assert "needs.public-lane.result != 'success'" in body and "exit 1" in body, (
        f"the first step of test-and-package ({first_step.group(1)!r}) must fail the job when "
        "the lane did not pass, before any gated step runs"
    )


def test_every_prefix_still_names_tests() -> None:
    for name, prefixes in lane_shards.SHARDS.items():
        for prefix in prefixes:
            if prefix.endswith("/"):
                held = list((REPO_ROOT / prefix).rglob("test_*.py"))
            else:
                parent = (REPO_ROOT / prefix).parent
                held = list(parent.glob(Path(prefix).name + "*.py"))
            assert held, (
                f"slice {name!r} claims {prefix!r}, which holds no test files any more. "
                "Its tests now run in `rest`; move the prefix and re-measure the balance "
                "(tests/lane_shards.py says how)."
            )


def test_no_prefix_is_claimed_twice() -> None:
    """A tie would go to whichever slice the dict lists first, silently."""
    claimed = [p for prefixes in lane_shards.SHARDS.values() for p in prefixes]
    assert len(claimed) == len(set(claimed)), claimed


@pytest.mark.parametrize(
    ("nodeid", "owner"),
    [
        ("tests/permissions_v2/test_evidence.py::test_x", "permissions_v2"),
        # the longer prefix wins over tests/permissions_v2/
        ("tests/permissions_v2/test_message_search_index.py::test_x", "message_search_gap"),
        ("tests/gap/p3/engine/test_x.py::TestY::test_z[a-b]", "message_search_gap"),
        ("tests/features/test_x.py::test_y", "features_topos_core"),
        ("tests/topos/test_x.py::test_y", "features_topos_core"),
        ("tests/core/test_x.py::test_y", "features_topos_core"),
        ("tests/query/test_x.py::test_y", "rest"),
        ("tests/test_profiles.py::test_x", "rest"),
        # a directory prefix does not claim a sibling that merely shares its name
        ("tests/topos_home_pin_test.py::test_x", "rest"),
        # added after the table was written: runs in `rest`, never dropped
        ("tests/a_directory_added_later/test_x.py::test_y", "rest"),
    ],
)
def test_the_most_specific_prefix_owns_an_item(nodeid: str, owner: str) -> None:
    assert lane_shards.shard_of(nodeid) == owner


class _Config:
    def __init__(self, shard):
        self._shard = shard
        self.deselected: list = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))

    def getoption(self, name):
        assert name == "lane_shard"
        return self._shard


def _items(*nodeids):
    return [SimpleNamespace(nodeid=n) for n in nodeids]


def test_a_slice_keeps_what_it_owns_and_deselects_the_rest() -> None:
    items = _items(
        "tests/permissions_v2/test_evidence.py::test_x",
        "tests/permissions_v2/test_message_search_state.py::test_x",
        "tests/query/test_x.py::test_y",
    )
    config = _Config("rest")
    lane_shards.deselect_other_shards(config, items)
    assert [i.nodeid for i in items] == ["tests/query/test_x.py::test_y"]
    assert len(config.deselected) == 2


def test_without_the_option_nothing_is_deselected() -> None:
    items = _items("tests/permissions_v2/test_evidence.py::test_x", "tests/query/test_x.py::test_y")
    config = _Config(None)
    lane_shards.deselect_other_shards(config, items)
    assert len(items) == 2 and config.deselected == []


def test_the_slices_partition_any_set_of_items() -> None:
    """Every item goes to exactly one slice, so running every slice runs every item
    once; the only exception is the guard in EVERY_SLICE, which runs in each."""
    guard = "tests/test_lane_shards.py::test_the_ci_matrix_names_every_slice_exactly_once"
    nodeids = [
        "tests/permissions_v2/test_evidence.py::test_x",
        "tests/permissions_v2/test_message_search_state.py::test_x",
        "tests/gap/p1/engine/test_x.py::test_y",
        "tests/features/test_x.py::test_y",
        "tests/query/test_x.py::test_y",
        "tests/test_profiles.py::test_x",
        "tests/a_directory_added_later/test_x.py::test_y",
    ]
    kept = []
    for shard in lane_shards.SHARDS:
        items = _items(*nodeids, guard)
        lane_shards.deselect_other_shards(_Config(shard), items)
        assert guard in [i.nodeid for i in items], shard
        kept.extend(i.nodeid for i in items if i.nodeid != guard)
    assert sorted(kept) == sorted(nodeids)


def test_this_guard_runs_in_every_slice() -> None:
    """Placed in one slice, it could not see that slice taken out of the matrix."""
    here = f"tests/{Path(__file__).name}::test_this_guard_runs_in_every_slice"
    assert here.startswith(lane_shards.EVERY_SLICE)
    for shard in lane_shards.SHARDS:
        items = _items(here)
        lane_shards.deselect_other_shards(_Config(shard), items)
        assert len(items) == 1, shard
