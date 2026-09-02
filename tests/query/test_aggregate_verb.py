"""The ``aggregate`` verb's envelope contract, driven through real dispatch.

protects: a deterministic number reaches the caller inside a validated
public_result with truthful narrowing; grantees and third-party principals
get a denial that carries no scope-shaped information; empties name their
cause.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import THIRD_PARTY, OWNER_APP, Principal
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.canonical.conversations_tables import ensure_all_tables

pytestmark = pytest.mark.asyncio

OWNER = Principal(cls=OWNER_APP, channel="cp_relay")
THIRD = Principal(cls=THIRD_PARTY, channel="cp_relay")


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "verb.db"))
    apply_all_migrations(c)
    ensure_all_tables(c)
    c.executemany(
        "INSERT INTO conversation_messages"
        " (message_id, conversation_id, dataset_id, sender_id, event_at, content, source_id)"
        " VALUES (?,?,?,?,?,?, 'imessage')",
        [
            (f"m{i}", "conv1", "u1:default", "+15125550100", f"2026-08-0{1 + i % 3}T10:00:00", "hi")
            for i in range(6)
        ],
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    return c


def _msg(payload, caller=None):
    m = {"id": "req-1", "type": "aggregate", "payload": payload}
    if caller is not None:
        m["caller"] = caller
    return m


async def test_scalar_count_full_envelope(conn):
    resp = await handle_control_plane_request(
        _msg({"scope_id": "messages:read", "measure": "count", "dataset_id": "u1:default"},
             caller={"mcp_source": "topos_home_chat"}),
        principal=OWNER,
    )
    assert resp["status"] == "ok"
    p = resp["payload"]
    assert p["turn_outcome"] == "live_query"
    pr = p["public_result"]
    assert pr["answer_type"] == "aggregate"
    assert pr["rows"] == [{"value": 6}]
    assert "empty_cause" not in pr
    ledger = p["narrowing"]["ledger"]
    assert any(e["reason"] == "aggregate_lane" for e in ledger)
    assert p["narrowing"]["result_empty"] is False
    assert p["query_session_id"].startswith("agg_")


async def test_grantee_flag_is_denied_shape(conn):
    resp = await handle_control_plane_request(
        _msg({"scope_id": "messages:read", "measure": "count", "is_grantee_request": True}),
        principal=OWNER,
    )
    p = resp["payload"]
    assert p["turn_outcome"] == "denied"
    assert p["deny_reason"] == "aggregate_principal_denied"
    assert p["public_result"] is None
    assert p["narrowing"]["result_empty"] is True
    assert p["narrowing"]["empty_cause"] == "scope_denied"


async def test_third_party_principal_denied(conn):
    resp = await handle_control_plane_request(
        _msg({"scope_id": "messages:read", "measure": "count"}),
        principal=THIRD,
    )
    assert resp["payload"]["deny_reason"] == "aggregate_principal_denied"
    # The denial names no scope, no table, no measure anywhere public.
    import json

    text = json.dumps(resp["payload"])
    assert "conversation_messages" not in text


async def test_param_error_is_denied_with_reason(conn):
    resp = await handle_control_plane_request(
        _msg({"scope_id": "messages:read", "measure": "median"}),
        principal=OWNER,
    )
    p = resp["payload"]
    assert p["turn_outcome"] == "denied"
    assert p["deny_reason"] == "aggregate_param_invalid"
    assert p["narrowing"]["empty_cause"] == "gate_vetoed"


async def test_unsupported_scope_reason(conn):
    resp = await handle_control_plane_request(
        _msg({"scope_id": "wormholes:read", "measure": "count"}),
        principal=OWNER,
    )
    assert resp["payload"]["deny_reason"] == "aggregate_scope_unsupported"


async def test_zero_group_result_names_no_match(conn):
    resp = await handle_control_plane_request(
        _msg(
            {
                "scope_id": "messages:read",
                "measure": "count",
                "group_by": "message_type",
                "since": "2030-01-01T00:00:00",
            },
            caller={"mcp_source": "topos_home_chat"},
        ),
        principal=OWNER,
    )
    p = resp["payload"]
    assert p["turn_outcome"] == "live_query"
    assert p["public_result"]["rows"] == []
    assert p["public_result"]["empty_cause"] == "no_match"
    assert p["narrowing"]["result_empty"] is True


async def test_missing_table_names_store_empty(conn):
    conn.execute("DROP TABLE conversation_messages")
    conn.commit()
    resp = await handle_control_plane_request(
        _msg({"scope_id": "messages:read", "measure": "count"},
             caller={"mcp_source": "topos_home_chat"}),
        principal=OWNER,
    )
    pr = resp["payload"]["public_result"]
    assert pr["rows"] == []
    assert pr["empty_cause"] == "store_empty"


async def test_forbidden_keys_never_in_public_result(conn):
    """Severed-wire adjacent: the envelope walk runs on this path — poison the
    builder output and the verb must refuse to ship it."""
    import topos.core.handlers.aggregate as agg_handler

    real = agg_handler.run_aggregate

    def poisoned(*a, **k):
        out = real(*a, **k)
        out["evidence"] = ["raw row text"]
        return out

    agg_handler.run_aggregate = poisoned
    try:
        resp = await handle_control_plane_request(
            _msg({"scope_id": "messages:read", "measure": "count"},
                 caller={"mcp_source": "topos_home_chat"}),
            principal=OWNER,
        )
    finally:
        agg_handler.run_aggregate = real
    assert resp["status"] == "error"
    assert "evidence" not in str(resp.get("payload") or "")
