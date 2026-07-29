"""Handler-path tests for the complexity message types (native port).

Exercises the real @handles message path against a live-shaped in-memory DB,
including payload clamping against hostile input.
"""

from __future__ import annotations

import pytest


def test_complexity_handlers_registered() -> None:
    import topos.core.handlers  # noqa: F401  (side-effect handler registration)
    from topos.core.handlers.registry import HANDLERS

    for message_type in ("complexity_summary", "complexity_timeline", "complexity_influence"):
        assert message_type in HANDLERS


@pytest.mark.asyncio
async def test_complexity_handlers_message_path(monkeypatch) -> None:
    import topos.core.handlers as hub
    from topos.core.handlers.registry import HANDLERS

    from tests.features.test_complexity import _conn, _seed_live_shaped

    conn = _conn()
    _seed_live_shaped(conn)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    summary_result = await HANDLERS["complexity_summary"](
        {"id": "req1", "type": "complexity_summary", "payload": {"recompute": True}}
    )
    assert summary_result["status"] == "ok", summary_result
    payload = summary_result["payload"]
    assert 0.0 < payload["focus_index"]["score"] < 100.0
    assert set(payload["readings"]) == {
        "current_focus",
        "structural_clarity",
        "information_breadth",
        "pipeline_confidence",
    }

    timeline_result = await HANDLERS["complexity_timeline"](
        {"id": "req2", "type": "complexity_timeline", "payload": {}}
    )
    assert timeline_result["status"] == "ok"
    assert timeline_result["payload"]["timeline"]

    influence_result = await HANDLERS["complexity_influence"](
        {"id": "req3", "type": "complexity_influence", "payload": {"target": "Aurora"}}
    )
    assert influence_result["status"] == "ok"
    threads = influence_result["payload"]["threads"]
    assert threads
    assert all(t["target_label"] == "Aurora" for t in threads)
    # target mode carries the counterfactual reading
    assert all("counterfactual_impact" in t["components"] for t in threads)

    # hostile payload integers are clamped, not crashed on / obeyed
    hostile = await HANDLERS["complexity_summary"](
        {
            "id": "req4",
            "type": "complexity_summary",
            "payload": {"recompute": True, "weeks": "abc", "window_days": -5},
        }
    )
    assert hostile["status"] == "ok"
    assert hostile["payload"]["params"]["weeks"] == 12
    assert hostile["payload"]["params"]["window_days"] == 1


def test_complexity_migration_creates_snapshot_table() -> None:
    import sqlite3

    from topos.storage.db.migrations import COMPLEXITY_V1_ID, apply_complexity_v1_up

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_complexity_v1_up(conn)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='complexity_snapshots'"
    ).fetchone()
    assert row is not None
    applied = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?", (COMPLEXITY_V1_ID,)
    ).fetchone()
    assert applied is not None
    # idempotent re-run
    apply_complexity_v1_up(conn)
