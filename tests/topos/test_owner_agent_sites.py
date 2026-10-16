"""The sites an ``owner_agent`` request passes on its way to an answer (AI-tool on-ramp, CONTRACTS §1.3 and
§1.5; lane L1).

protects: Off-limits — an entry the owner made stays hidden from the agent (the guard treats the class as a
caller it cannot place) while the agent reads the owner's own view of what the upgrade carried, exactly as the
owner's outside client under his own key; the ``aggregate`` and ``query`` handlers serve the agent and each
write one request-log row that names it by its verified client id; the query audit event carries the client
id; the owner's request counts account for the agent by its verified id and the counts route serves the class.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import topos.core.handlers as hub
import topos.core.state as state
import topos.query.runtime as runtime
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.common import set_engine_config_value
from topos.features.lifecycle.blackhole import EVERYONE, OWNER
from topos.features.lifecycle.blackhole_guard import guard_from_message
from topos.features.lifecycle.off_limits_view import for_request, is_another_person, is_owner_himself
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER_UI = Principal(cls=OWNER_APP, channel="uds")
OWNER_ID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "sites.db"), check_same_thread=False)
    apply_all_migrations(c)
    set_engine_config_value(c, "user_id", OWNER_ID)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


def _owner_made_entry(conn, *, entity_id="ent-1", name="Someone Private") -> None:
    conn.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, created_by)"
        " VALUES ('bh-1', ?, ?, ?, 'owner')",
        (entity_id, name.lower(), name),
    )
    conn.commit()


# ------------------------------------------------------------------ Off-limits
def test_owner_agent_is_the_owners_own_outside_client_for_the_view():
    assert is_owner_himself(AGENT) is True
    assert for_request(AGENT, current=False) == OWNER
    assert is_another_person(AGENT) is False
    # Minted on the socket only: the same class on any other channel is nobody the node can place.
    relayed = Principal(cls="owner_agent", channel="cp_relay", client_id="agent-claude-code")
    assert is_owner_himself(relayed) is False
    assert for_request(relayed, current=False) == EVERYONE


@pytest.mark.asyncio
async def test_an_entry_the_owner_made_is_hidden_from_owner_agent(conn):
    _owner_made_entry(conn)
    token = set_principal(AGENT)
    try:
        guard = guard_from_message(conn, {"id": "r1", "type": "query", "payload": {}})
    finally:
        reset_principal(token)
    assert guard.caller_class == "unknown"            # entries apply, as for a third party
    assert guard.view == OWNER                         # but what the upgrade carried is read as his own
    assert guard.blocks_entity_id("ent-1") is True
    assert guard.blocks_name("Someone Private") is True
    assert guard.filter_rows([{"entity_id": "ent-1"}, {"entity_id": "ent-2"}]) == [{"entity_id": "ent-2"}]
    # Control: the owner's own UI still sees the entry.
    token = set_principal(OWNER_UI)
    try:
        ui = guard_from_message(conn, {"id": "r1", "type": "query", "payload": {}, "caller": {"mcp_source": "topos_home_chat"}})
    finally:
        reset_principal(token)
    assert ui.blocks_entity_id("ent-1") is False


# ------------------------------------------------------------------ aggregate
@pytest.mark.asyncio
async def test_aggregate_serves_owner_agent_and_writes_one_audit_row_naming_it(conn):
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    ensure_all_tables(conn)
    conn.executemany(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, event_at, content, source_id)"
        " VALUES (?,?,?,?,?,?, 'imessage')",
        [(f"m{i}", "conv1", "u1:default", "+15125550100", f"2026-08-0{1 + i % 3}T10:00:00", "hi") for i in range(6)],
    )
    conn.commit()
    resp = await handle_control_plane_request(
        {"id": "agg-1", "type": "aggregate", "payload": {"scope_id": "messages:read", "measure": "count", "dataset_id": "u1:default"}},
        principal=AGENT,
    )
    assert resp["status"] == "ok", resp
    assert resp["payload"]["turn_outcome"] == "live_query"
    assert resp["payload"]["public_result"]["rows"] == [{"value": 6}]
    rows = [tuple(r) for r in conn.execute(
        "SELECT tool_name, source, requester_id, access_context, verified_cls, verified_client_id FROM mcp_request_log"
    ).fetchall()]  # get_user_id leaves the connection on sqlite3.Row, which never equals a tuple
    assert rows == [("aggregate", "claude-code", OWNER_ID, "owner_self", "owner_agent", "agent-claude-code")]


@pytest.mark.asyncio
async def test_aggregate_still_refuses_a_third_party(conn):
    resp = await handle_control_plane_request(
        {"id": "agg-2", "type": "aggregate", "payload": {"scope_id": "messages:read", "measure": "count"}},
        principal=Principal(cls=THIRD_PARTY, channel="local_http", client_id="cursor"),
    )
    assert resp["payload"]["turn_outcome"] == "denied"
    assert resp["payload"]["deny_reason"] == "aggregate_principal_denied"


# ------------------------------------------------------------------ query
class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.kwargs: Optional[Dict[str, Any]] = None

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        self.kwargs = kwargs
        return {"turn_outcome": "live_query", "public_result": {"summaries": []}, "session_id": kwargs.get("query_session_id")}


@pytest.mark.asyncio
async def test_query_writes_one_audit_row_with_the_agents_client_id(conn, monkeypatch):
    recorder = _RecordingOrchestrator()
    monkeypatch.setattr(runtime, "get_query_orchestrator", lambda **_: recorder)
    state._ensure_mcp_request_log_table(conn)
    resp = await handle_control_plane_request(
        {"id": "q-1", "type": "query", "payload": {"scope_id": "messages:read", "intent": "what did we plan", "query_session_id": "qs-1"}},
        principal=AGENT,
    )
    assert resp["status"] == "ok", resp
    assert recorder.kwargs is not None and recorder.kwargs["scope_id"] == "messages:read"
    rows = [tuple(r) for r in conn.execute(
        "SELECT tool_name, source, requester_id, access_context, verified_cls, verified_client_id FROM mcp_request_log"
    ).fetchall()]  # get_user_id leaves the connection on sqlite3.Row, which never equals a tuple
    assert rows == [("query", "claude-code", OWNER_ID, "owner_self", "owner_agent", "agent-claude-code")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal",
    [OWNER_UI, Principal(cls="cp_relay", channel="cp_relay"), Principal(cls="owner_automation", channel="cp_relay"), None],
    ids=["owner_app", "cp_relay", "owner_automation", "legacy_none"],
)
async def test_the_owners_own_queries_write_no_request_log_row(conn, monkeypatch, principal):
    """Home chat and relay queries are not MCP requests: the C5 gap was about agents and third parties."""
    recorder = _RecordingOrchestrator()
    monkeypatch.setattr(runtime, "get_query_orchestrator", lambda **_: recorder)
    state._ensure_mcp_request_log_table(conn)
    resp = await handle_control_plane_request(
        {"id": "q-2", "type": "query", "payload": {"scope_id": "messages:read", "intent": "what did we plan", "query_session_id": "qs-2"}},
        principal=principal,
    )
    assert resp["status"] == "ok", resp
    assert conn.execute("SELECT COUNT(*) FROM mcp_request_log").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_query_audit_event_carries_the_client_id(conn):
    from topos.storage.adapters.sqlite.stores import SQLiteAuditLogStore

    token = set_principal(AGENT)
    try:
        event_id = SQLiteAuditLogStore(conn).append({"session_id": "qs-1", "event_type": "query", "scope_id": "messages:read"})
    finally:
        reset_principal(token)
    row = conn.execute("SELECT client_id, payload_json FROM query_audit_events WHERE event_id = ?", (event_id,)).fetchone()
    assert row[0] == "agent-claude-code"
    assert '"client_id": "agent-claude-code"' in row[1]
    page = SQLiteAuditLogStore(conn).query(session_id="qs-1")
    assert page.items[0]["client_id"] == "agent-claude-code"


@pytest.mark.parametrize("factory", [None, sqlite3.Row], ids=["plain", "Row"])
def test_an_agents_request_log_row_leaves_the_connections_row_factory_as_it_found_it(factory):
    """Naming the owner on the agent's row reads engine_config; that read must not flip the caller's
    connection to sqlite3.Row (or back) as a side effect."""
    c = sqlite3.connect(":memory:")
    c.row_factory = factory
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute("INSERT INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER_ID,))
    c.commit()
    token = set_principal(AGENT)
    try:
        state.record_mcp_request(c, "query", source=None, requester_id=None)
    finally:
        reset_principal(token)
    assert c.row_factory is factory
    row = c.execute("SELECT source, requester_id, access_context FROM mcp_request_log").fetchone()
    assert tuple(row) == ("claude-code", OWNER_ID, "owner_self")
    c.close()


# ------------------------------------------------------------------ counts
def test_request_counts_account_for_the_agent_by_its_verified_id(conn):
    token = set_principal(AGENT)
    try:
        state.record_mcp_request(conn, "query", source="claude-code", requester_id=OWNER_ID, resource_owner_user_id=OWNER_ID)
        state.record_mcp_request(conn, "aggregate", source="claude-code", requester_id=OWNER_ID, resource_owner_user_id=OWNER_ID)
    finally:
        reset_principal(token)
    counts = state.get_mcp_request_counts(conn, 30)
    assert counts["by_client"] == [{"client_id": "agent-claude-code", "principal_cls": "owner_agent", "count": 2}]
    assert counts["by_access_context"] == [{"access_context": "owner_self", "count": 2}]


@pytest.mark.parametrize(
    "principal, status",
    [
        (AGENT, 200),
        (OWNER_UI, 200),
        (Principal(cls=THIRD_PARTY, channel="local_http", client_id="cursor"), 403),
    ],
    ids=["owner_agent", "owner_app", "third_party"],
)
def test_the_counts_route_serves_the_owner_and_the_agent_and_refuses_a_third_party(monkeypatch, principal, status):
    from topos.api import usage
    from topos.auth import resolve_request_principal

    async def fake_dispatch(msg, *, principal=None):
        return {"status": "ok", "payload": {"uma": {}, "mcp": {"total": 0, "by_client": []}}}

    monkeypatch.setattr(usage, "handle_control_plane_request", fake_dispatch)
    app = FastAPI()
    app.include_router(usage.router)
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    with TestClient(app) as client:
        resp = client.get("/api/request-counts")
    assert resp.status_code == status
