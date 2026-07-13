"""Guard: the I-series introspection catalog keeps its contract
(PLAN_MCP_INTROSPECTION.md §6).

Pins the properties the CP runner and the report lanes depend on: unique ids,
valid lanes, the regression flagship (I-C1), guard-lane precision pins, and
pure checkers (dict in → (bool, str) out; no engine imports, no IO).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from introspection_eval_cases import (  # noqa: E402
    INTROSPECTION_CASES,
    INTROSPECTION_CATALOG_VERSION,
    LANES,
    cases_by_lane,
)


def test_catalog_version_is_stamped() -> None:
    assert INTROSPECTION_CATALOG_VERSION.startswith("qq-introspect-")


def test_case_ids_unique_and_lanes_valid() -> None:
    ids = [c.id for c in INTROSPECTION_CASES]
    assert len(ids) == len(set(ids)), f"duplicate case ids: {ids}"
    assert all(c.lane in LANES for c in INTROSPECTION_CASES)


def test_flagship_regression_case_present() -> None:
    flagship = next(c for c in INTROSPECTION_CASES if c.id == "I-C1")
    assert flagship.tool == "query_scope"
    assert "github" in str(flagship.args.get("intent", "")).lower()


def test_guard_lane_has_precision_pins() -> None:
    assert len(cases_by_lane()["guard"]) >= 3


def test_absent_lane_never_empty() -> None:
    assert len(cases_by_lane()["absent"]) >= 3


def test_checkers_are_pure_over_dicts() -> None:
    """Every checker must tolerate an empty payload and return (bool, str) —
    the runner feeds it raw tool output and must never crash the lane."""
    for case in INTROSPECTION_CASES:
        passed, reason = case.evaluate({})
        assert isinstance(passed, bool) and isinstance(reason, str), case.id
        assert passed is False, f"{case.id} passed on an EMPTY payload: {reason}"


def test_mcp_only_tools() -> None:
    allowed = {
        "describe_connector",
        "list_connectors",
        "list_scopes",
        "mcp_session_context",
        "query_scope",
    }
    assert {c.tool for c in INTROSPECTION_CASES} <= allowed
