"""The legacy-inspection floor: unprojected rows and counts leave the node only on the owner's lanes.

protects: six tools return raw substrate (``get_table_rows``, ``get_messages``,
``get_oplog``, ``get_analytics``, ``read_jsonl_file``, ``list_jsonl_files``) and
four return names, shapes and counts (``list_database_tables``,
``get_table_schema``, ``get_table_count``, ``graph_summary``). Released main had no gate on any of
them at the dispatcher: a THIRD_PARTY principal — an enrolled ``tpk_`` client on
the local door, or any bearer of the shared key over TCP — reached every
handler, and the only refusal on the beta lineage fired when the owner had
already set an off-limits entity, so a fresh node had none.

The floor is decided by the channel-verified class, never by the payload:

* ``OWNER_APP`` (the socket) is served.
* ``THIRD_PARTY`` is refused on all ten, unconditionally, on every channel —
  including a relay message the control plane stamped ``third_party``.
* The control-plane relay deferral (``CP_RELAY``: the owner's hosted web app
  and its sharing card), the routine lane (``owner_automation``) and the legacy
  no-principal mode are served, but the off-limits floor still applies to them:
  once the owner has black-holed anything, only the socket reads these tools.

Every refusal is the same object: ``{"code": 403, "error": "owner_mode_required"}``.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.registry import HANDLERS
from topos.principal import (
    CP_RELAY,
    OWNER_APP,
    RELAY_PRINCIPAL,
    THIRD_PARTY,
    Principal,
)

pytestmark = pytest.mark.asyncio

ROW_TOOLS = [
    "get_table_rows",
    "get_messages",
    "get_oplog",
    "get_analytics",
    "read_jsonl_file",
    "list_jsonl_files",
]
METADATA_TOOLS = ["list_database_tables", "get_table_schema", "get_table_count", "graph_summary"]
ALL_TOOLS = ROW_TOOLS + METADATA_TOOLS

OWNER = Principal(cls=OWNER_APP, channel="uds")
TPK_CLIENT = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")
SHARED_KEY_TCP = Principal(cls=THIRD_PARTY, channel="remote_http")
STAMPED_THIRD_PARTY = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="claude_desktop")
AUTOMATION = Principal(cls="owner_automation", channel="cp_relay", client_id="routine_executor")
LEGACY: Optional[Principal] = None

PRINCIPALS = {
    "owner_app": OWNER,
    "third_party_tpk": TPK_CLIENT,
    "third_party_tcp": SHARED_KEY_TCP,
    "third_party_stamped_relay": STAMPED_THIRD_PARTY,
    "cp_relay": RELAY_PRINCIPAL,
    "owner_automation": AUTOMATION,
    "legacy_none": LEGACY,
}
SERVED_WHEN_QUIET = {"owner_app", "cp_relay", "owner_automation", "legacy_none"}

REFUSAL = {"status": "error", "code": 403, "error": "owner_mode_required"}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "node.sqlite"), check_same_thread=False)
    apply_all_migrations(c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


@pytest.fixture()
def reached(monkeypatch) -> List[str]:
    """Replace the ten handlers with probes so the test sees the gate, not the handler."""
    seen: List[str] = []

    def _probe(msg_type: str):
        async def handler(message: Dict[str, Any]) -> Dict[str, Any]:
            seen.append(msg_type)
            return {"id": message.get("id"), "status": "ok", "payload": {"probe": msg_type}}

        return handler

    for msg_type in ALL_TOOLS:
        assert msg_type in HANDLERS, msg_type
        monkeypatch.setitem(HANDLERS, msg_type, _probe(msg_type))
    return seen


def _blackhole_something(conn: sqlite3.Connection) -> None:
    from topos.features.lifecycle.blackhole import BlackholeStore
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard

    BlackholeStore(conn).blackhole_entity(entity_ref="Quenn Zorblat-Ix")
    assert BlackholeGuard(conn).active


async def _dispatch(msg_type: str, principal: Optional[Principal]) -> Dict[str, Any]:
    return await handle_control_plane_request(
        {"id": f"req-{msg_type}", "type": msg_type, "payload": {"table_name": "entities"}},
        principal=principal,
    )


def _assert_refused(out: Dict[str, Any], msg_type: str) -> None:
    assert out == {"id": f"req-{msg_type}", **REFUSAL}, out


# ---------------------------------------------------------------- the matrix


@pytest.mark.parametrize("msg_type", ALL_TOOLS)
@pytest.mark.parametrize("who", list(PRINCIPALS), ids=list(PRINCIPALS))
async def test_fresh_node_tool_by_principal(conn, reached, msg_type: str, who: str) -> None:
    """A node whose owner never set an off-limits entity: the class alone decides."""
    out = await _dispatch(msg_type, PRINCIPALS[who])

    if who in SERVED_WHEN_QUIET:
        assert reached == [msg_type], out
        assert out["status"] == "ok"
    else:
        assert reached == [], f"{who} reached {msg_type}"
        _assert_refused(out, msg_type)


@pytest.mark.parametrize("msg_type", ALL_TOOLS)
@pytest.mark.parametrize("who", list(PRINCIPALS), ids=list(PRINCIPALS))
async def test_offlimits_node_tool_by_principal(conn, reached, msg_type: str, who: str) -> None:
    """Once anything is black-holed, only the socket reads unprojected substrate."""
    _blackhole_something(conn)

    out = await _dispatch(msg_type, PRINCIPALS[who])

    if who == "owner_app":
        assert reached == [msg_type], out
    else:
        assert reached == [], f"{who} reached {msg_type} past the off-limits floor"
        _assert_refused(out, msg_type)


@pytest.mark.parametrize("msg_type", ALL_TOOLS)
async def test_no_database_refuses_every_non_owner(monkeypatch, reached, msg_type: str) -> None:
    """With no connection the off-limits floor cannot be read, so the relay is refused."""
    monkeypatch.setattr(hub, "get_db_connection", lambda: None)

    assert (await _dispatch(msg_type, RELAY_PRINCIPAL)) == {"id": f"req-{msg_type}", **REFUSAL}
    assert reached == []
    assert (await _dispatch(msg_type, OWNER))["status"] == "ok"
    assert reached == [msg_type]


async def test_refusal_is_one_object_for_every_third_party(conn, reached) -> None:
    """No tool, channel or client id changes the refusal: nothing to fingerprint."""
    bodies = set()
    for msg_type in ALL_TOOLS:
        for principal in (TPK_CLIENT, SHARED_KEY_TCP, STAMPED_THIRD_PARTY):
            out = await _dispatch(msg_type, principal)
            out.pop("id")
            bodies.add(tuple(sorted(out.items())))
    assert bodies == {tuple(sorted(REFUSAL.items()))}
    assert reached == []


async def test_other_tools_are_not_gated(conn, reached) -> None:
    """The floor names ten tools; an eleventh handler still sees a third party."""
    seen: List[str] = []

    async def other(message):
        seen.append(message["type"])
        return {"id": message.get("id"), "status": "ok", "payload": {}}

    HANDLERS["_legacy_gate_other"] = other
    try:
        out = await _dispatch("_legacy_gate_other", TPK_CLIENT)
        assert out["status"] == "ok" and seen == ["_legacy_gate_other"]
    finally:
        HANDLERS.pop("_legacy_gate_other", None)


# ------------------------------------------------------------ the HTTP door


@pytest.fixture()
def local_door(conn):
    from topos.api.local_mcp import router
    from topos.auth import resolve_request_principal

    app = FastAPI()
    app.include_router(router)

    def _as(principal: Principal) -> TestClient:
        app.dependency_overrides[resolve_request_principal] = lambda: principal
        return TestClient(app)

    return _as


def test_enrolled_client_on_the_local_door_gets_the_refusal(local_door) -> None:
    """A tpk_ client is THIRD_PARTY on local_http; both local metadata routes refuse it."""
    client = local_door(TPK_CLIENT)

    tables = client.post("/api/local/list_database_tables")
    schema = client.post("/api/local/get_table_schema", json={"table_name": "entities"})

    assert tables.status_code == 200 and tables.json() == {"status": "error", "error": "owner_mode_required"}
    assert schema.status_code == 200 and schema.json() == {"status": "error", "error": "owner_mode_required"}


def test_owner_socket_on_the_local_door_reads_tables(local_door) -> None:
    client = local_door(OWNER)

    tables = client.post("/api/local/list_database_tables")

    assert tables.status_code == 200
    assert "tables" in tables.json(), tables.json()


# ---------------------------------------------------- the sharing card (CP relay)


def _cp_forward_message(msg_type: str) -> Dict[str, Any]:
    """The message ``_forward_owner_scoped`` builds for the sharing card's row counts.

    Shape read from control_plane/mcp_gateway.py: the gateway merges ``mcp_source``
    and ``mcp_requester_id`` into the payload and adds a ``caller`` block. With no
    stamp key deployed no ``principal_stamp`` is attached, so the node resolves it
    to RELAY_PRINCIPAL exactly as app.py's ``_relay_dispatch`` does.
    """
    return {
        "id": "sharing-card-1",
        "type": msg_type,
        "payload": {"mcp_source": "claude_desktop", "mcp_requester_id": "user-owner"},
        "caller": {"mcp_source": "claude_desktop", "requester_id": "user-owner"},
    }


async def _relay_dispatch(message: Dict[str, Any]) -> Dict[str, Any]:
    from topos.relay_stamp import verify_relay_stamp

    principal = verify_relay_stamp(message) or RELAY_PRINCIPAL
    return await handle_control_plane_request(message, principal=principal)


async def test_sharing_card_row_counts_still_arrive_over_the_relay(conn) -> None:
    """Real handler, real schema: the control plane's list_database_tables relay keeps working."""
    out = await _relay_dispatch(_cp_forward_message("list_database_tables"))

    assert out["status"] == "ok", out
    tables = out["payload"]["tables"]
    groups = list(tables.values()) if isinstance(tables, dict) else [tables]
    names = {t["name"]: t for group in groups for t in group if isinstance(t, dict)}
    assert "entities" in names, sorted(names)
    assert isinstance(int(names["entities"].get("row_count") or 0), int)


async def test_sharing_card_relay_is_refused_once_the_owner_black_holes(conn) -> None:
    _blackhole_something(conn)

    out = await _relay_dispatch(_cp_forward_message("list_database_tables"))

    assert out == {"id": "sharing-card-1", **REFUSAL}


async def test_a_stamped_third_party_relay_never_reads_rows(conn, reached) -> None:
    """What the same MCP forward becomes once the stamp key is deployed: refused."""
    message = _cp_forward_message("get_table_rows")

    out = await handle_control_plane_request(message, principal=STAMPED_THIRD_PARTY)

    assert out == {"id": "sharing-card-1", **REFUSAL}
    assert reached == []


def test_the_ten_tools_are_the_decided_set() -> None:
    """The gate's own lists: the nine the owner decided on 22 Sep 2026 plus get_table_count."""
    from topos.core.handlers import (
        LEGACY_INSPECTION_METADATA_TYPES,
        LEGACY_INSPECTION_ROW_TYPES,
    )

    assert set(LEGACY_INSPECTION_ROW_TYPES) == set(ROW_TOOLS)
    assert set(LEGACY_INSPECTION_METADATA_TYPES) == set(METADATA_TOOLS)
    assert CP_RELAY == RELAY_PRINCIPAL.cls
