"""A grantee's legacy `places:read` query gets disclosed place names, never raw ones.

`place_name` is a PII field (`PII_DISCLOSURE_FIELDS["location_events"]`): the
ingest privacy layer writes `place_name_disclosure`, because a place name is a
home address as often as a cafe. `uma_get_rows` and the in-memory adapter serve
that copy below the owner's tier. The SQLite list spec for `location_events` had
no disclosure variant, so the legacy query door served the RAW place name to any
grantee holding `places:read`, and matched the grantee's words against it.

The same door also answers the question about the journal parent a location row
points at (`_canonical_row_to_item` reads it raw with `SELECT *`): that read is
gated on `journal_entries` being in the same manifest as `location_events`, and
no registry scope lists both (nor does the SQLite location spec carry the
`source_record_id` it follows), so through this door it never runs for anyone.
The last test pins the registry half, so the day a scope gains both tables the
parent read is looked at before it ships.

Driven through `handle_control_plane_request` with the payload the control plane
builds for `shared_query_scope`, over a node's SQLite adapters. Rows are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, Principal

RAW_PLACE = "quillfeather Wrenfield Row"
DISCLOSED_PLACE = "quillfeather [ADDRESS]"
UNDISCLOSED_RAW_PLACE = "quillfeather Marrowgate Yard"
PARENT_PROSE = "supper with Ottoline about the heronmoss move"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "places.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, content_disclosure, content_nsfw, source_id, entry_at)"
        " VALUES ('je-parent', ?, ?, 0, 'demo_places_file', '2026-07-01T19:00:00Z')",
        (PARENT_PROSE, PARENT_PROSE),
    )
    c.execute(
        "INSERT INTO location_events (event_id, place_name, place_name_disclosure, city, event_at, source_id,"
        " source_record_id) VALUES"
        " ('loc-disclosed', ?, ?, 'Springfield', '2026-07-01T19:00:00Z', 'demo_places_file', 'je-parent'),"
        " ('loc-pending', ?, NULL, 'Springfield', '2026-07-02T19:00:00Z', 'demo_places_file', NULL)",
        (RAW_PLACE, DISCLOSED_PLACE, UNDISCLOSED_RAW_PLACE),
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(query: str, *, grantee: bool = True, mode: str = "summary") -> dict:
    payload = {
        "scope_id": "places:read",
        "access_mode": mode,
        "intent": query,
        "query": query,
        "query_session_id": f"places-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            is_grantee_request=True,
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="grantee-a",
        )
        if mode == "summary":
            payload["filter_manifest"] = {"access_mode_ceiling": "summary"}
        # A raw ask is a grant with no filters: the control plane forwards none.
        principal = RELAY_PRINCIPAL
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


@pytest.mark.asyncio
async def test_a_grantee_gets_the_disclosed_place_name(conn):
    payload = await _ask("springfield")  # the city, which both rows share and which is not a PII field
    assert payload.get("disclosure_tier") == "default_disclosure"
    wire = json.dumps(payload, default=str)
    assert DISCLOSED_PLACE in wire, "the disclosed copy is what a grantee reads"
    assert "[disclosure pending]" in wire, "a place name with no disclosed copy reads as pending"
    assert "Wrenfield" not in wire and "Marrowgate" not in wire, "a raw place name reached a grantee"
    # The journal parent: its gate (`journal_entries` in the manifest) is never met under places:read.
    assert "Ottoline" not in wire and "heronmoss" not in wire


@pytest.mark.asyncio
async def test_a_grantee_cannot_match_on_the_raw_place_name(conn):
    """The `contains` filter runs over the disclosed copy: a raw street name is not a probe."""
    payload = await _ask("Wrenfield")
    wire = json.dumps(payload, default=str)
    assert "loc-disclosed" not in wire and "Wrenfield" not in wire


@pytest.mark.asyncio
async def test_raw_mode_lists_disclosed_place_names_too(conn):
    """A grant with no lower ceiling reaches rows directly; the disclosure sits in the store, so it holds."""
    payload = await _ask("springfield", mode="raw")
    rows = (payload.get("public_result") or {}).get("rows") or []
    assert rows, payload
    wire = json.dumps(payload, default=str)
    assert DISCLOSED_PLACE in wire and "Wrenfield" not in wire and "Marrowgate" not in wire


@pytest.mark.asyncio
async def test_the_owner_still_reads_raw_place_names(conn):
    payload = await _ask("quillfeather", grantee=False)
    assert payload.get("disclosure_tier") == "owner_raw"
    wire = json.dumps(payload, default=str)
    assert "Wrenfield" in wire and "Marrowgate" in wire


def test_no_column_means_pending_never_raw(conn):
    from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore

    store = SQLiteCanonicalStore(conn)
    conn.execute("ALTER TABLE location_events DROP COLUMN place_name_disclosure")
    rows = store.list("location_events", disclosure_tier="default_disclosure").items
    assert sorted(row["place_name"] for row in rows) == ["[disclosure pending]", "[disclosure pending]"]
    assert all(row["content"] == "[disclosure pending]" for row in rows)


def test_no_registry_scope_lists_a_location_row_with_its_journal_parent():
    """The gate on the raw parent read in `_canonical_row_to_item` is unsatisfiable through the door.

    `resolve_scope_manifest` builds the manifest from ONE registry scope, and a grant's
    `scope_table_allowlist` only narrows it. If this fails, a scope now holds both tables and the
    parent's `SELECT *` (raw content, people, place_name, no disclosure tier, no NSFW check, no
    Off-limits filter) runs for grantees wherever the location row carries `source_record_id`: the
    in-memory and Postgres adapters return it, the SQLite list spec does not. Give the parent the
    journal row's own rules first.
    """
    from topos.query.manifest_validation import ManifestValidationError, resolve_scope_manifest
    from topos.query.scope_registry_loader import list_scopes

    both, resolved = [], 0
    for entry in list_scopes():
        scope_id = entry["scope_id"]
        allowlist = {"scope_table_allowlist": {scope_id: ["location_events", "journal_entries"]}}
        try:
            manifests = (resolve_scope_manifest(scope_id), resolve_scope_manifest(scope_id, filter_manifest=allowlist))
        except ManifestValidationError:
            continue  # the door refuses this id (a deprecated legacy scope), so no query runs under it
        resolved += 1
        for manifest in manifests:
            if {"location_events", "journal_entries"} <= set(manifest.canonical_tables):
                both.append(scope_id)
    assert resolved >= 20 and both == []
