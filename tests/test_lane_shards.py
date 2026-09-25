"""CI runs the public lane as slices; together they must still be the whole lane.

ci.yml's job matrix runs one job per slice of tests/lane_shards.py. Two ways
that silently stop testing something, and neither turns anything red on its
own: a slice defined in the table but missing from the matrix (its tests never
run anywhere), and a prefix that no longer names a real path after a rename
(its tests fall to `rest`, which still runs them, but the balance the table
was measured for is gone and nobody is told). This file fails on both, and
pins the ownership rule that makes the slices a partition.
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
    """Every item goes to exactly one slice, so running every slice runs every item once."""
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
        items = _items(*nodeids)
        lane_shards.deselect_other_shards(_Config(shard), items)
        kept.extend(i.nodeid for i in items)
    assert sorted(kept) == sorted(nodeids)
