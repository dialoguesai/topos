"""A grantee's message summary marks another person's words without naming them.

On a first-person ask the canonical lane prefixes another person's message with a
speaker label, so nothing they said can read as the owner's words. The label came
from `_sender_display`: `contacts.display_name` through `contact_identifiers`, a
table a `messages:read` grant does not cover, or, for a sender with no contact
row, the raw `sender_id` (a phone number or email address). In a grantee's summary
that put a real name in front of a body whose disclosure had masked the name, and
put the raw handle in `speaker_label`, a key the grantee scrub never reads.

The topic-thread roster already holds the rule (`_thread_participants`): names are
the owner's, and below the owner's tier a counterparty is marked, not named.

Driven through the real `query` door with the payload the control plane builds for
`shared_query_scope`. The handles and names are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

#: First person, not a belief ask (which would drop other people's rows) and not an
#: interaction ask (which adds a contacts lane of its own).
ASK = "what have I been working on about quillfeather"
HANDLE = "kestrel.marsh@example.org"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "speaker.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    ensure_all_tables(c)
    CanonicalTablesManager(c)
    c.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content,"
        " content_disclosure, content_nsfw, event_at, source_id, is_from_self) VALUES"
        " ('cm-named', 'cv-a', 'ds-a', 'handle-wren', 'quillfeather plan from Ottoline',"
        "  'quillfeather plan from [NAME]', 0, '2026-07-01T19:00:00Z', 'imessage', 0),"
        " ('cm-handle', 'cv-a', 'ds-a', ?, 'quillfeather draft from Kestrel',"
        "  'quillfeather draft from [NAME]', 0, '2026-07-01T20:00:00Z', 'imessage', 0),"
        " ('cm-mine', 'cv-a', 'ds-a', 'self', 'quillfeather reply mine', 'quillfeather reply mine', 0,"
        "  '2026-07-02T19:00:00Z', 'imessage', 1)",
        (HANDLE,),
    )
    c.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)"
        " VALUES ('ct-wren', 'ds-a', 'imessage', 'Ottoline Wrenfield', 0)"
    )
    c.execute(
        "INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id)"
        " VALUES ('ds-a', 'imessage', 'handle-wren', 'handle', 'ct-wren')"
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(*, grantee: bool) -> dict:
    payload = {
        "scope_id": "messages:read",
        "access_mode": "summary",
        "intent": ASK,
        "query": ASK,
        "query_session_id": f"speaker-{uuid.uuid4().hex[:8]}",
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


def _summaries(payload: dict) -> list:
    return list((payload.get("public_result") or {}).get("summaries") or [])


@pytest.mark.asyncio
async def test_a_grantee_reads_another_persons_words_marked_but_unnamed(conn):
    payload = await _ask(grantee=True)
    assert payload.get("disclosure_tier") == "default_disclosure"
    wire = json.dumps(payload, default=str)
    assert "Ottoline" not in wire and "Wrenfield" not in wire, "a contact's name reached a grantee"
    assert HANDLE not in wire and "kestrel.marsh" not in wire, "a raw sender handle reached a grantee"
    others = [s for s in _summaries(payload) if "[NAME]" in str(s.get("summary_text"))]
    assert len(others) == 2, "both of the other person's messages still answer"
    for item in others:
        assert item["summary_text"].startswith("[someone else] "), item
        assert item.get("speaker_label") == "someone else"
    mine = [s for s in _summaries(payload) if "reply mine" in str(s.get("summary_text"))]
    assert mine and "speaker_label" not in mine[0], "the owner's own words carry no speaker"


@pytest.mark.asyncio
async def test_the_owner_still_sees_who_said_it(conn):
    payload = await _ask(grantee=False)
    labels = {s.get("speaker_label") for s in _summaries(payload)}
    assert "Ottoline Wrenfield" in labels and HANDLE in labels
