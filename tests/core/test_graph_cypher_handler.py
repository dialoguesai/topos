"""ws-bridge handler for owner-only openCypher (graph_cypher_query, §4)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import EDGE_SEMANTIC_AFFINITY, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def wired(monkeypatch):
    import topos.core.state as state

    # :memory: (not a temp file) so the injected handle is the one the hub hands
    # back — a file-backed handle is re-resolved against settings.topos_database_path.
    # check_same_thread=False because the handler runs in asyncio.to_thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    apply_all_migrations(conn)
    resolver = EntityResolver(conn)
    ada = resolver._create_entity("Ada", "person")
    cass = resolver._create_entity("Cass", "person")
    conn.commit()
    update_edge(
        conn,
        src_entity_id=ada,
        dst_entity_id=cass,
        edge_type=EDGE_SEMANTIC_AFFINITY,
        increment=0.7,
    )
    conn.commit()

    monkeypatch.setattr(state, "db_conn", conn)
    monkeypatch.setattr(state, "_db_conn_path", None)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_affinity_traversal_over_ws_bridge(wired):
    from topos.core.handlers import handle_control_plane_request

    reply = await handle_control_plane_request(
        {
            "id": "r1",
            "type": "graph_cypher_query",
            "payload": {
                "query": (
                    "MATCH (a:person)-[r:semantic_affinity]->(b:person) "
                    "RETURN a.canonical_name, b.canonical_name, r.weight"
                )
            },
        }
    )
    assert reply["status"] == "ok"
    names = {
        (row["a.canonical_name"], row["b.canonical_name"]) for row in reply["payload"]["rows"]
    }
    assert ("Ada", "Cass") in names and ("Cass", "Ada") in names


@pytest.mark.asyncio
async def test_malformed_query_is_a_clean_400(wired):
    from topos.core.handlers import handle_control_plane_request

    reply = await handle_control_plane_request(
        {"id": "r2", "type": "graph_cypher_query", "payload": {"query": "MATCH bogus ((("}}
    )
    assert reply["status"] == "error"
    assert reply["code"] == 400
    assert reply["error"] and "\n" not in reply["error"]


@pytest.mark.asyncio
async def test_missing_query_is_an_error_not_a_crash(wired):
    from topos.core.handlers import handle_control_plane_request

    reply = await handle_control_plane_request(
        {"id": "r3", "type": "graph_cypher_query", "payload": {}}
    )
    assert reply["status"] == "error"
    assert reply["code"] == 400
