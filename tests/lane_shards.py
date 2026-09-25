"""Named slices of the public lane, so CI can run it as a job matrix.

The lane is one pytest process over ~10k tests. On 2026-09-25 it took 38m49s
on CI for main c2a77019 (run 36167805662), and six merges inside two minutes
started six of those in parallel. ``--lane-shard NAME`` keeps the items one
slice owns and deselects the rest, and ci.yml runs one job per slice.

Deselected AFTER collection, not by passing paths: every shard imports exactly
the modules the unsplit lane imports, so a test only ever differs from the
unsplit lane in which tests ran before it in its process, never in what was
imported. It also keeps ci.yml's lane command identical to `just gate`'s plus
one flag, which is what tests/test_local_gate_composition.py reads.

Ownership is by nodeid prefix, the most specific prefix wins, and ``rest`` owns
every item no prefix claims. So the slices partition the lane by construction:
a new directory or file is never dropped, it lands in ``rest`` until someone
moves it. ci.yml's matrix must name every slice here exactly once;
tests/test_lane_shards.py fails when the two disagree, and it runs in EVERY
slice (``EVERY_SLICE``), the one exception to the partition.

Balanced on CI wall clock: six runs of the unsplit lane on 2026-09-25, each
`-q` progress line's time split evenly over its 72 tests and summed per file,
median per file. permissions_v2 alone was 17.4 of ~37 test-minutes, so its
message-search family gets a slice of its own. Measured that way the slices
were 9.5 (permissions_v2), 9.0 (message_search_gap), 9.6
(features_topos_core) and 8.8 (rest) minutes. Rebalance when one runs far
longer than the others or nears the job's timeout-minutes.

Not pytest-xdist, which is the cheaper split on paper. This suite's own guards
(tests/conftest.py: module state leaked between tests, engine threads that
outlived their test, owner-data reach) record findings in the process that ran
the test and red the run from ``pytest_sessionfinish``. Under xdist those run
in a worker, and a worker's ``session.exitstatus`` never reaches the
controller: a toy suite with that shape exited 1 serially and 0 under
``-n 2`` (xdist 3.8.0, pytest 9.0.3). The lane would pass with a leak live.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pytest

OPTION = "--lane-shard"

#: The slice that owns whatever no prefix below claims.
REST = "rest"

#: Slice name -> the nodeid prefixes it owns (nodeids are rootdir-relative).
#: Directory prefixes keep their trailing slash, so "tests/topos/" does not
#: also claim tests/topos_home_pin.py.
SHARDS: Dict[str, Tuple[str, ...]] = {
    "permissions_v2": ("tests/permissions_v2/",),
    "message_search_gap": (
        "tests/permissions_v2/test_message_search",
        "tests/gap/",
    ),
    "features_topos_core": ("tests/features/", "tests/topos/", "tests/core/"),
    REST: (),
}

#: Runs in every slice, not only in the one that owns it: the guard that ci.yml's
#: matrix still names every slice. Run by one slice only, taking THAT slice out
#: of the matrix would take the guard with it, and CI would go green with a
#: quarter of the lane unrun. About twenty sub-second tests, so four copies cost
#: nothing.
EVERY_SLICE: Tuple[str, ...] = ("tests/test_lane_shards.py",)


def shard_of(nodeid: str) -> str:
    """The slice that owns ``nodeid``: longest matching prefix, else ``rest``."""
    owner, matched = REST, -1
    for name, prefixes in SHARDS.items():
        for prefix in prefixes:
            if nodeid.startswith(prefix) and len(prefix) > matched:
                owner, matched = name, len(prefix)
    return owner


def addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        OPTION,
        choices=sorted(SHARDS),
        default=None,
        help="run one slice of the collected tests (tests/lane_shards.py); "
        "CI's job matrix runs every slice",
    )


def deselect_other_shards(config: pytest.Config, items: List[pytest.Item]) -> None:
    """Keep the items the requested slice owns, plus ``EVERY_SLICE``; report the
    rest as deselected."""
    wanted: Optional[str] = config.getoption("lane_shard")
    if wanted is None:
        return
    keep: List[pytest.Item] = []
    drop: List[pytest.Item] = []
    for item in items:
        mine = shard_of(item.nodeid) == wanted or item.nodeid.startswith(EVERY_SLICE)
        (keep if mine else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
