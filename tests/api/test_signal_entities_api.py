"""Entity spine read API (/v1/signal/entities*) — People tab backend."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.entities.edges import EDGE_CO_OCCURRENCE, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def populated_conn(tmp_path):
    # check_same_thread=False: /entities/graph runs graph work in asyncio.to_thread.
    conn = sqlite3.connect(str(tmp_path / "entities_api.db"), check_same_thread=False)
    apply_all_migrations(conn)
    resolver = EntityResolver(conn)
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)"
        " VALUES ('c-maya', 'ds', 'src', 'Maya Chen', 0)"
    )
    conn.commit()
    resolver.seed_from_contacts()
    maya, _ = resolver.resolve("Maya Chen", entity_type="person")
    org, _ = resolver.resolve("Mudlark Studio", entity_type="org")
    for i in range(3):
        resolver.record_mention(
            maya, record_id=f"rec-{i}", surface_text="Maya Chen",
            canonical_table="conversation_messages", source_id="src",
            event_at=f"2026-06-0{i + 1}T10:00:00Z", confidence=0.9,
        )
    resolver.record_mention(
        org, record_id="rec-0", surface_text="Mudlark Studio",
        canonical_table="journal_entries", source_id="src",
        event_at="2026-06-01T10:00:00Z", confidence=0.9,
    )
    update_edge(conn, src_entity_id=maya, dst_entity_id=org,
                edge_type=EDGE_CO_OCCURRENCE, event_at="2026-06-01T10:00:00Z")
    conn.commit()
    yield conn, maya
    conn.close()


@pytest.fixture()
def client_ctx(populated_conn, monkeypatch):
    conn, maya = populated_conn
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)

    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    yield app, maya
    app.dependency_overrides.pop(require_api_key, None)


async def _get(app, path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers={"Authorization": "Bearer test-key"})


@pytest.mark.asyncio
async def test_list_entities_sorted_and_typed(client_ctx) -> None:
    app, _maya = client_ctx
    resp = await _get(app, "/v1/signal/entities?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["canonical_name"] == "Maya Chen"  # most mentions first
    assert body["items"][0]["mention_count"] == 3
    assert body["items"][0]["is_contact"] is True
    assert body["type_counts"] == {"person": 1, "org": 1}


@pytest.mark.asyncio
async def test_list_entities_search_and_type_filter(client_ctx) -> None:
    app, _maya = client_ctx
    resp = await _get(app, "/v1/signal/entities?q=mudlark")
    assert [i["canonical_name"] for i in resp.json()["items"]] == ["Mudlark Studio"]
    resp = await _get(app, "/v1/signal/entities?entity_type=person")
    assert [i["canonical_name"] for i in resp.json()["items"]] == ["Maya Chen"]
    resp = await _get(app, "/v1/signal/entities?q=nobody-by-this-name")
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_entity_detail_includes_mentions_and_connections(client_ctx) -> None:
    app, maya = client_ctx
    resp = await _get(app, f"/v1/signal/entities/{maya}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_name"] == "Maya Chen"
    assert len(body["recent_mentions"]) == 3
    # most recent first
    assert body["recent_mentions"][0]["event_at"] == "2026-06-03T10:00:00Z"
    assert body["connections"][0]["entity_name"] == "Mudlark Studio"
    assert body["connections"][0]["edge_type"] == "co_occurrence"
    assert body["dossier"] is None or isinstance(body["dossier"], dict)


@pytest.mark.asyncio
async def test_entity_detail_unknown_404(client_ctx) -> None:
    app, _maya = client_ctx
    resp = await _get(app, "/v1/signal/entities/ent_does_not_exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_entity_graph_shape_and_route_precedence(client_ctx) -> None:
    """/entities/graph must not be captured by /entities/{entity_id}."""
    app, _maya = client_ctx
    resp = await _get(app, "/v1/signal/entities/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert {n["label"] for n in body["nodes"]} == {"Maya Chen", "Mudlark Studio"}
    assert len(body["edges"]) == 1
    assert body["edges"][0]["edge_type"] == "co_occurrence"


@pytest.mark.asyncio
async def test_entities_requires_auth(populated_conn, monkeypatch) -> None:
    conn, _maya = populated_conn
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
    from topos.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/signal/entities")
    assert resp.status_code == 401
