"""REST surface for owner fact verdicts: POST /v1/facts/verdict."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.facts.store import FactStore
from topos.storage.db.migrations import apply_all_migrations
from topos.uds import UDSChannelApp

pytestmark = pytest.mark.public


@pytest.fixture()
def client_ctx(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "verdicts_api.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent_self', 'person', 'Ada Voss', 'ada voss', 1)"
    )
    conn.commit()

    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)

    from topos.app import app
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_key", "test-key")
    monkeypatch.setattr(settings, "topos_owner_key", "owner-test-key")
    try:
        yield app, conn
    finally:
        conn.close()


async def _post(app, body: dict, *, owner_transport=True, bearer=None):
    # Exercise the real resolver through the same trusted ASGI transport wrapper
    # used by the owner socket. Do not bypass the owner dependency with a key.
    transport = ASGITransport(app=UDSChannelApp(app) if owner_transport else app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/signal/facts/verdict",
            json=body,
            headers=({"Authorization": f"Bearer {bearer}"} if bearer else {})
                | {"X-Topos-Client": "topos-home-chat/1", "X-Topos-Transport": "uds"},
        )


@pytest.mark.asyncio
async def test_confirm_then_reject_flow(client_ctx) -> None:
    app, conn = client_ctx
    fact = FactStore(conn).assert_fact(
        subject_entity_id="ent_self", predicate="lives_in",
        object_value="Brooklyn", confidence=0.55,
    )

    resp = await _post(app, {"object_id": fact["object_id"], "action": "confirm"})
    assert resp.status_code == 200
    assert resp.json()["payload"]["confidence"] == 1.0

    # The list surface exposes the verified state for the review UI.
    transport = ASGITransport(app=UDSChannelApp(app))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/v1/signal/facts", headers={"Authorization": "Bearer test-key"}
        )
    assert listed.status_code == 200
    (item,) = listed.json()["items"]
    assert item["verified_by_owner"] is True
    assert item["confidence"] == 1.0

    resp = await _post(app, {"object_id": fact["object_id"], "action": "reject"})
    assert resp.status_code == 200
    assert resp.json()["facts_closed"] == 1


@pytest.mark.asyncio
async def test_edit_and_errors(client_ctx) -> None:
    app, conn = client_ctx
    fact = FactStore(conn).assert_fact(
        subject_entity_id="ent_self", predicate="works_on",
        object_value="paywall UI", confidence=0.55,
    )

    resp = await _post(
        app,
        {"object_id": fact["object_id"], "action": "edit", "object_value": "billing UI"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"]["object_value"] == "billing UI"
    assert body["payload"]["verified_by_owner"] is True

    resp = await _post(app, {"object_id": "missing", "action": "confirm"})
    assert resp.status_code == 404

    resp = await _post(app, {"object_id": fact["object_id"], "action": "promote"})
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bearer,status", [("test-key", 403), ("owner-test-key", 403), (None, 401)])
async def test_tcp_credentials_and_forged_owner_markers_cannot_read_or_edit_facts(client_ctx, bearer, status):
    app, conn = client_ctx
    fact = FactStore(conn).assert_fact(subject_entity_id="ent_self", predicate="prefers",
        object_value="synthetic reading topic", confidence=0.55)
    before = conn.execute("SELECT payload_json,confidence,valid_to FROM signal_objects WHERE object_id=?", (fact["object_id"],)).fetchone()
    response = await _post(app, {"object_id": fact["object_id"], "action": "reject", "requester_is_owner": True},
        owner_transport=False, bearer=bearer)
    assert response.status_code == status
    assert conn.execute("SELECT payload_json,confidence,valid_to FROM signal_objects WHERE object_id=?", (fact["object_id"],)).fetchone() == before
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/signal/facts", headers={"Authorization": f"Bearer {bearer}"} if bearer else {})
    assert response.status_code == status
    assert "synthetic reading topic" not in response.text
