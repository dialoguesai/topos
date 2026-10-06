"""Below the owner's tier, the vector and recent lanes serve only rows the grant could read itself.

Both lanes read `signal_embeddings`, which is chosen by SOURCE, not by table, and
hand the stored text to the summary:

* a scope with no sources (attention, facts, complexity, interests) ran the recent
  lane unscoped, so its grantee read the last fortnight of every table's text;
* a source that writes two tables (a journal export writes `journal_entries` and
  their `location_events` children) put journal prose in front of a `places:read`
  grantee, the leak the parent-row read in `_canonical_row_to_item` is gated against;
* neither lane read the owner's NSFW decision, so a flagged message's indexed copy
  reached a `messages:read` grantee.

The canonical lane keeps both rules (only `manifest.canonical_tables`, never a
flagged row); `_index_hits_inside_grant` applies them to index rows. Driven through
the real `query` door as the control plane forwards a grantee's `shared_query_scope`.
The vector search itself is stubbed at the signal service (a real one needs an
embedding model); it answers from the same index rows. Rows are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

#: A journal export installed for the journal and its place children, as a runtime source.
SHARED_SOURCE = "fieldnotes_journal_export"
TOKENS = ("INDEXPLAIN", "INDEXFLAGGED", "INDEXJOURNAL", "INDEXPLACE")


def _seed(c: sqlite3.Connection, *, days_ago: int) -> None:
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    c.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content,"
        " content_disclosure, content_nsfw, event_at, source_id, is_from_self) VALUES"
        " ('cm-plainrow', 'cv-a', 'ds-a', 's', 'quillfeather raw plain', 'quillfeather INDEXPLAIN note', 0, ?, 'imessage', 1),"
        " ('cm-withheldrow', 'cv-a', 'ds-a', 's', 'quillfeather raw flagged', 'quillfeather INDEXFLAGGED note', 1, ?,"
        " 'imessage', 1)",
        (at, at),
    )
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, content_disclosure, content_nsfw, source_id, entry_at)"
        " VALUES ('je-shared', 'quillfeather raw journal', 'quillfeather INDEXJOURNAL entry', 0, ?, ?)",
        (SHARED_SOURCE, at),
    )
    c.execute(
        "INSERT INTO location_events (event_id, place_name, place_name_disclosure, event_at, source_id, source_record_id)"
        " VALUES ('je-shared-loc', 'quillfeather INDEXPLACE', 'quillfeather INDEXPLACE', ?, ?, 'je-shared')",
        (at, SHARED_SOURCE),
    )
    for rid, source, dim, text, record_type in (
        ("cm-plainrow", "imessage", "messages", "quillfeather INDEXPLAIN note", "conversation_message"),
        ("cm-withheldrow", "imessage", "messages", "quillfeather INDEXFLAGGED note", "conversation_message"),
        ("je-shared", SHARED_SOURCE, "wellbeing", "quillfeather INDEXJOURNAL entry", "journal_entry"),
        # A location child carries no record_type in the index: its table cannot be named.
        ("je-shared-loc", SHARED_SOURCE, "places", "quillfeather INDEXCHILDCOPY", None),
        # An untyped index row with no canonical twin: nothing names its table.
        ("idx-untyped", SHARED_SOURCE, "places", "quillfeather INDEXUNTYPED", None),
    ):
        c.execute(
            "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,"
            " text_preview, search_text, chunk_index, event_at, record_type, created_at)"
            " VALUES (?, ?, ?, ?, 'stub-model', 'stub', 3, ?, ?, 0, ?, ?, ?)",
            (f"emb-{rid}", rid, source, dim, text, text, at, record_type, at),
        )
    c.execute(
        "CREATE TABLE IF NOT EXISTS source_runtime_installs (install_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL,"
        " source_id TEXT NOT NULL, version_id TEXT, status TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0,"
        " source_definition_json TEXT NOT NULL, source_version_row_json TEXT, failure_reason TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    for install_id, source_id, definition in (
        ("inst-journal", SHARED_SOURCE,
         {"default_scope_id": "health:read", "allowed_scope_ids": ["health:read", "places:read"]}),
        ("inst-imessage", "imessage", {"default_scope_id": "messages:read", "allowed_scope_ids": ["messages:read"]}),
    ):
        c.execute(
            "INSERT INTO source_runtime_installs (install_id, scope_key, source_id, status, is_active,"
            " source_definition_json, created_at, updated_at) VALUES (?, 'k', ?, 'installed', 1, ?, ?, ?)",
            (install_id, source_id, json.dumps(definition), at, at),
        )
    c.commit()


def _open(tmp_path, monkeypatch, *, days_ago: int) -> sqlite3.Connection:
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "index-lanes.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    ensure_all_tables(c)
    CanonicalTablesManager(c)
    _seed(c, days_ago=days_ago)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    return c


@pytest.fixture()
def recent(tmp_path, monkeypatch):
    """Index rows from yesterday: the recent lane carries them."""
    c = _open(tmp_path, monkeypatch, days_ago=1)
    yield c
    c.close()


@pytest.fixture()
def vector(tmp_path, monkeypatch):
    """Index rows from two months ago (outside the recent lane's fortnight), served by a stubbed vector search."""
    c = _open(tmp_path, monkeypatch, days_ago=60)
    import topos.features.signal.service as service

    class _Search:
        def search_vectors(self, *, query, limit, source_id=None, event_after=None, event_before=None):
            rows = c.execute(
                "SELECT record_id, text_preview, search_text, source_id, signal_dimension, event_at, record_type"
                " FROM signal_embeddings"
            ).fetchall()
            items = [
                {"record_id": r[0], "text_preview": r[1], "search_text": r[2], "similarity": 0.9,
                 "source_id": r[3], "signal_dimension": r[4], "event_at": r[5], "record_type": r[6]}
                for r in rows
                if source_id is None or r[3] == source_id
            ]
            return {"items": items[:limit]}

    monkeypatch.setattr(service, "get_signal_service", lambda: _Search())
    yield c
    c.close()


async def _ask(scope: str, *, grantee: bool = True, ask: str = "quillfeather") -> dict:
    payload = {
        "scope_id": scope,
        "access_mode": "summary",
        "intent": ask,
        "query": ask,
        "query_session_id": f"index-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            filter_manifest={"access_mode_ceiling": "summary"},
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="owner-a",
        )
        principal = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="outside-client", acting_user="owner-a")
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


def _present(payload: dict) -> set:
    wire = json.dumps(payload, default=str)
    return {token for token in TOKENS if token in wire}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["attention:read", "facts:read", "complexity:read", "interests:read"])
async def test_a_scope_without_tables_reads_no_index_text(recent, scope):
    # A plain recency ask: the recent lane is what answers it.
    found = _present(await _ask(scope, ask="what happened recently"))
    assert found == set(), f"{scope}: a grantee read other tables' indexed text"


@pytest.mark.asyncio
async def test_a_messages_grantee_reads_only_unflagged_messages_from_the_index(recent):
    assert _present(await _ask("messages:read")) == {"INDEXPLAIN"}


@pytest.mark.asyncio
async def test_a_places_grantee_reads_no_journal_prose_from_a_shared_source(recent):
    payload = await _ask("places:read")
    found = _present(payload)
    assert "INDEXJOURNAL" not in found, "journal prose reached a location grant through the index"
    assert "INDEXPLACE" in found, "the location row itself still answers, through the canonical lane"
    # An index row with no record_type cannot be shown to be inside the grant, so it is dropped.
    wire = json.dumps(payload, default=str)
    assert "INDEXCHILDCOPY" not in wire and "INDEXUNTYPED" not in wire


@pytest.mark.asyncio
async def test_the_vector_lane_keeps_the_same_rules(vector):
    assert _present(await _ask("messages:read")) == {"INDEXPLAIN"}
    assert "INDEXJOURNAL" not in _present(await _ask("places:read"))
    assert _present(await _ask("facts:read")) == set()


@pytest.mark.asyncio
async def test_the_owner_still_reads_every_row(recent):
    """The owner's tier is unchanged: the flagged message and the journal entry still answer."""
    wire = json.dumps(await _ask("messages:read", grantee=False), default=str)
    assert "raw flagged" in wire and "raw plain" in wire
    recent_owner = json.dumps(await _ask("attention:read", grantee=False, ask="what happened recently"), default=str)
    assert "INDEXJOURNAL" in recent_owner, "the owner's recent lane is unscoped by design"


def test_a_hit_is_kept_only_when_its_row_shows_it_is_unflagged():
    """The fail-closed branches, which a node's own schema never reaches through the door."""
    from topos.query.manifest import ScopeResolutionManifest
    from topos.query.retrieval import _index_hits_inside_grant
    from topos.storage.adapters.fakes import InMemoryCanonicalStore

    store = InMemoryCanonicalStore()
    store.upsert("conversation_messages", {"record_id": "plain", "content_nsfw": 0})
    store.upsert("conversation_messages", {"record_id": "flagged", "content_nsfw": 1})
    store.upsert("conversation_messages", {"record_id": "no-flag-column"})
    manifest = ScopeResolutionManifest(scope_id="messages:read", primary_dimensions=[],
                                       canonical_tables=["conversation_messages"])
    hits = [{"record_id": rid, "record_type": "conversation_message"}
            for rid in ("plain", "flagged", "no-flag-column", "no-row")]
    kept = _index_hits_inside_grant(hits, conn=None, manifest=manifest, disclosure_tier="default_disclosure",
                                    canonical=store)
    assert [h["record_id"] for h in kept] == ["plain"]
    assert _index_hits_inside_grant(hits, conn=None, manifest=manifest, disclosure_tier="owner_raw",
                                    canonical=store) == hits
    # No store and no connection: no flag can be read.
    assert _index_hits_inside_grant(hits, conn=None, manifest=manifest, disclosure_tier="default_disclosure") == []
