"""A focused run keeps normal collection and refuses stale/empty test mappings."""
import json
from types import SimpleNamespace

import pytest

from tests import release_check_selection as selection


def config(tmp_path, plan, shard=None):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    options = {"release_check_plan": str(path), "lane_shard": shard}
    dropped = []
    return SimpleNamespace(getoption=lambda key: options[key],
                           hook=SimpleNamespace(pytest_deselected=lambda items: dropped.extend(items))), dropped


def test_targeted_run_deselects_only_after_normal_collection(tmp_path):
    c, dropped = config(tmp_path, {"schema": 1, "mode": "targeted", "targets": ["tests/topos/test_status_cmd.py"]})
    held = SimpleNamespace(nodeid="tests/topos/test_status_cmd.py::test_status")
    other = SimpleNamespace(nodeid="tests/query/test_query.py::test_query")
    items = [held, other]
    selection.select(c, items)
    assert items == [held] and dropped == [other]


@pytest.mark.parametrize("plan,shard", [
    ({"schema": 1, "mode": "targeted", "targets": []}, None),
    ({"schema": 1, "mode": "targeted", "targets": ["tests/../bad.py"]}, None),
    ({"schema": 1, "mode": "targeted", "targets": ["tests/missing.py"]}, None),
    ({"schema": 1, "mode": "targeted", "targets": ["tests/a.py"]}, "rest"),
    ({"schema": 2, "mode": "targeted", "targets": ["tests/a.py"]}, None),
])
def test_invalid_or_uncollected_targets_fail(tmp_path, plan, shard):
    c, _ = config(tmp_path, plan, shard)
    with pytest.raises(pytest.UsageError):
        selection.select(c, [SimpleNamespace(nodeid="tests/a.py::test_a")])
