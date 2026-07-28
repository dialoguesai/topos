"""REST surface for owner fact verdicts: POST /v1/facts/verdict."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.facts.store import FactStore
from topos.storage.db.migrations import apply_all_migrations

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
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    yield app, conn
    app.dependency_overrides.pop(require_api_key, None)
    conn.close()


async def _post(app, body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/signal/facts/verdict",
            json=body,
            headers={"Authorization": "Bearer test-key"},
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
    transport = ASGITransport(app=app)
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
