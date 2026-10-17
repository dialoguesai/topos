"""Apply the planner after normal collection, preserving imports and leak guards."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def addoption(parser: pytest.Parser) -> None:
    parser.addoption("--release-check-plan", default=None, help="JSON from scripts/select_release_checks.py")


def select(config: pytest.Config, items: list[pytest.Item]) -> None:
    path = config.getoption("release_check_plan")
    if not path:
        return
    if config.getoption("lane_shard"):
        raise pytest.UsageError("A targeted plan and a lane shard cannot be combined")
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
        targets = plan["targets"]
        if plan["schema"] != 1 or plan["mode"] != "targeted" or not targets:
            raise ValueError("expected a nonempty targeted plan")
        if any(not isinstance(t, str) or not t.startswith("tests/") or ".." in t.split("/") for t in targets):
            raise ValueError("invalid test targets")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise pytest.UsageError(f"Invalid release check plan: {exc}") from exc
    keep = [item for item in items if str(item.nodeid).split("::", 1)[0] in targets]
    if not keep:
        raise pytest.UsageError("The targeted release plan selected no tests")
    missing = set(targets) - {str(item.nodeid).split("::", 1)[0] for item in keep}
    if missing:
        raise pytest.UsageError(f"Targeted plan names no collected tests: {sorted(missing)}")
    keep_ids = {id(item) for item in keep}
    dropped = [item for item in items if id(item) not in keep_ids]
    config.hook.pytest_deselected(items=dropped)
    items[:] = keep
