"""Same-device /api/local/verify_claim route: full handler round-trip against a
scratch DB, door policy asserted (app_id mandatory, mode pinned to fun)."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import topos.core.handlers as hub
from topos.api.local_mcp import router
from topos.auth import require_api_key


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations
    from topos.features.facts.store import FactStore

    conn = sqlite3.connect(str(tmp_path / "local.sqlite"), check_same_thread=False)
    apply_all_migrations(conn)
    FactStore(conn).assert_fact(
        subject_entity_id="self-1",
        predicate="enjoys",
        object_value="playing the mandolin",
        dimension="interests",
        confidence=0.8,
        source_refs=[],
        disclosure="scoped",
    )
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_key] = lambda: None
    test_client = TestClient(app)
    yield test_client
    conn.close()


def test_local_verify_claim_roundtrip(client):
    response = client.post(
        "/api/local/verify_claim",
        json={"statement": "I have never played the mandolin", "app_id": "truth-mirror"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "fun"
    assert data["lanes"]["self"]["stance"] == "contradicts"
    assert "_audit" not in data and "evidence" not in data
    assert "mandolin" not in str(data.get("lanes"))  # stances only, no text


def test_local_verify_claim_requires_app_id(client):
    response = client.post("/api/local/verify_claim", json={"statement": "I love pizza"})
    assert response.json().get("status") == "error"


def test_local_verify_claim_mode_is_pinned(client):
    # A caller-supplied mode is ignored by the route (it never reads one) —
    # even "serious" in the body still runs fun.
    response = client.post(
        "/api/local/verify_claim",
        json={"statement": "I love playing the mandolin", "app_id": "truth-mirror", "mode": "serious"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "fun"
