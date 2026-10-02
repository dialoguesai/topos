"""A grantee's "who do I talk to" reads only what the grant covers.

The interaction-browse lane (a first-person "who do I talk/chat with" ask) listed
`contacts` under whatever scope the turn ran, and added the relationship graph's
`communicates_with` neighbours of the owner by name. A `messages:read` grant
names neither table, yet its grantee was handed the owner's contact names and the
people the owner talks to. Below the owner's tier the lane now keeps the scope
ceiling every canonical lane keeps (contacts only when the manifest lists them)
and the graph lane's rule (relationship-graph names are the owner's).

Driven through the real `query` door with the payload the control plane builds for
`shared_query_scope`. Names are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, Principal

ASK = "who do I talk to most"
CONTACT = "Ottoline Wrenfield"  # observed by the messenger (source imessage)
BOOK_CONTACT = "Peregrine Hollins"  # from the address book, the source contacts:resolve covers
EDGE_PERSON = "Bramble Ashcombe"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "interaction.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    ensure_all_tables(c)
    CanonicalTablesManager(c)
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) VALUES"
        " ('ct-wren', 'ds-a', 'imessage', ?, 0),"
        " ('ct-book', 'ds-b', 'canonical_address_book', ?, 0)",
        (CONTACT, BOOK_CONTACT),
    )
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self) VALUES"
        " ('ent-owner', 'person', 'Owner', 'owner', 1),"
        " ('ent-bramble', 'person', ?, 'bramble ashcombe', 0)",
        (EDGE_PERSON,),
    )
    c.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count)"
        " VALUES ('edge-1', 'ent-owner', 'ent-bramble', 'communicates_with', 3.0, 3)"
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(scope: str, *, grantee: bool) -> dict:
    payload = {
        "scope_id": scope,
        "access_mode": "summary",
        "intent": ASK,
        "query": ASK,
        "query_session_id": f"interaction-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            is_grantee_request=True,
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            filter_manifest={"access_mode_ceiling": "summary"},
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="grantee-a",
        )
        principal = RELAY_PRINCIPAL
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


@pytest.mark.asyncio
async def test_a_messages_grantee_gets_no_contact_or_graph_names(conn):
    payload = await _ask("messages:read", grantee=True)
    assert payload.get("disclosure_tier") == "default_disclosure"
    wire = json.dumps(payload, default=str)
    assert "Ottoline" not in wire and "Wrenfield" not in wire, "a contact name reached a messages:read grantee"
    assert "Bramble" not in wire and "Ashcombe" not in wire, "a relationship-graph name reached a grantee"


@pytest.mark.asyncio
async def test_a_contacts_grantee_still_gets_the_contacts_it_was_granted(conn):
    """`contacts:resolve` lists the table: its names are the grant's own data, as in the canonical lane."""
    payload = await _ask("contacts:resolve", grantee=True)
    wire = json.dumps(payload, default=str)
    assert BOOK_CONTACT in wire
    assert "Bramble" not in wire, "the relationship graph is still the owner's"


@pytest.mark.asyncio
async def test_the_owner_still_hears_who_they_talk_to(conn):
    payload = await _ask("messages:read", grantee=False)
    wire = json.dumps(payload, default=str)
    assert f"Contact: {CONTACT}" in wire and f"Talked with: {EDGE_PERSON}" in wire
