"""P13 tests: control-plane WS handlers for the dense-intelligence reads.

These message types back the hosted-app proxy routes (control_plane
signal_proxy). Contract: every payload here must match the shape of the
corresponding HTTP route in topos/api/signal.py, because the frontend
consumes both transports through the same client code.
"""

from __future__ import annotations

import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.features.entities.consolidation import list_review, propose_merges
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "intelligence.db"))
    apply_all_migrations(c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


def _mk_entity(conn, name, etype="person", mentions=5, contact=None):
    resolver = EntityResolver(conn)
    entity_id = resolver._create_entity(name, etype, contact_id=contact)
    conn.execute(
        "UPDATE entities SET mention_count=? WHERE entity_id=?", (mentions, entity_id)
    )
    conn.commit()
    return entity_id


async def _send(msg_type: str, payload=None, req_id="req-1"):
    message = {"id": req_id, "type": msg_type}
    if payload is not None:
        message["payload"] = payload
    return await handle_control_plane_request(message)


@pytest.mark.asyncio
async def test_list_entities_payload_shape(conn) -> None:
    _mk_entity(conn, "Maya Chen", mentions=12)
    _mk_entity(conn, "Topos", etype="org", mentions=7)

    result = await _send("signal_list_entities", {"limit": 10})
    assert result["status"] == "ok"
    payload = result["payload"]
    assert payload["total"] == 2
    names = [item["canonical_name"] for item in payload["items"]]
    assert names[0] == "Maya Chen"  # sorted by mention count

    filtered = await _send("signal_list_entities", {"entity_type": "org"})
    assert [i["canonical_name"] for i in filtered["payload"]["items"]] == ["Topos"]


@pytest.mark.asyncio
async def test_get_entity_detail_and_not_found(conn) -> None:
    entity_id = _mk_entity(conn, "Maya Chen")

    result = await _send("signal_get_entity", {"entity_id": entity_id})
    assert result["status"] == "ok"
    assert result["payload"]["canonical_name"] == "Maya Chen"
    assert "recent_mentions" in result["payload"]

    missing = await _send("signal_get_entity", {"entity_id": "nope"})
    assert missing["status"] == "error"
    assert missing["code"] == 404

    blank = await _send("signal_get_entity", {})
    assert blank["status"] == "error"
    assert blank["code"] == 400


@pytest.mark.asyncio
async def test_entity_graph_shape(conn) -> None:
    _mk_entity(conn, "Maya Chen")
    result = await _send("signal_entity_graph", {"limit_nodes": 50})
    assert result["status"] == "ok"
    assert "nodes" in result["payload"] and "edges" in result["payload"]


@pytest.mark.asyncio
async def test_facts_insights_timeline_empty_shapes(conn) -> None:
    for msg_type in ("signal_list_facts", "signal_list_insights", "signal_list_timeline"):
        result = await _send(msg_type, {})
        assert result["status"] == "ok", msg_type
        assert result["payload"]["items"] == [], msg_type
        assert result["payload"]["total"] == 0, msg_type


@pytest.mark.asyncio
async def test_review_sweep_list_approve_roundtrip(conn) -> None:
    _mk_entity(conn, "Jon", mentions=65)
    _mk_entity(conn, "Jonathan", mentions=54)

    swept = await _send("signal_entity_review_sweep")
    assert swept["status"] == "ok"
    assert swept["payload"]["prefix"] == 1

    listed = await _send("signal_list_entity_review", {"status": "pending"})
    assert listed["status"] == "ok"
    items = listed["payload"]["items"]
    assert len(items) == 1 and listed["payload"]["total"] == 1

    review_id = items[0]["review_id"]
    acted = await _send(
        "signal_entity_review_action", {"review_id": review_id, "action": "approve"}
    )
    assert acted["status"] == "ok"
    assert list_review(conn, status="pending") == []

    # replay on a resolved review surfaces the engine's ValueError as a 400
    replay = await _send(
        "signal_entity_review_action", {"review_id": review_id, "action": "approve"}
    )
    assert replay["status"] == "error"
    assert replay["code"] == 400


@pytest.mark.asyncio
async def test_review_action_validation(conn) -> None:
    bad_action = await _send(
        "signal_entity_review_action", {"review_id": "rev-1", "action": "merge"}
    )
    assert bad_action["status"] == "error" and bad_action["code"] == 400

    missing = await _send(
        "signal_entity_review_action", {"review_id": "rev-missing", "action": "dismiss"}
    )
    assert missing["status"] == "error" and missing["code"] == 404


@pytest.mark.asyncio
async def test_exclude_entity(conn) -> None:
    entity_id = _mk_entity(conn, "LL", mentions=2)

    result = await _send("signal_exclude_entity", {"entity_id": entity_id})
    assert result["status"] == "ok"
    assert result["payload"]["entity_found"] is True
    assert result["payload"]["exclusion_id"]

    blank = await _send("signal_exclude_entity", {})
    assert blank["status"] == "error" and blank["code"] == 400


@pytest.mark.asyncio
async def test_missing_request_id_returns_none(conn) -> None:
    result = await handle_control_plane_request(
        {"type": "signal_list_entities", "payload": {}}
    )
    assert result is None
