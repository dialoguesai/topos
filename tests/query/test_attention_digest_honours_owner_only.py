"""An outside owner client never reads a triage digest its writer marked owner-only.

`features/triage/daily.py` builds each day's digest out of the related rows
themselves: a message's first 80 characters become a "missed-but-matters" title,
a journal entry's place and people and a location's place name become the
interest vocabulary. Those rows never passed their own table's disclosure or
NSFW check on the way in, and the writer says so on every object it stores:
`disclosure: owner_only`. `_fact_disclosure_allowed` is the rule every other
derived object on this path follows, and the attention lane never asked it, so a
lower-tier client holding `attention:read` read raw message text.

The digest here is written by the triage's own reader and writer from an invented
message, then read through the real `query` door as an enrolled outside client.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

RAW = "quillfeather meet at Marrowgate Yard by the heronmoss ferry"
DISCLOSED = "quillfeather meet at [ADDRESS] by the heronmoss ferry"
DAY = "2026-07-01"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.features.triage.daily import _write_signal_objects, load_triage_delta
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "attention.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    ensure_all_tables(c)
    CanonicalTablesManager(c)  # re-runs the always-run migrations over the messenger tables just created
    c.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content,"
        " content_disclosure, content_nsfw, event_at, source_id, is_from_self)"
        " VALUES ('cm-digest', 'cv-a', 'ds-a', 'sender-a', ?, ?, 0, ?, 'imessage', 0)",
        (RAW, DISCLOSED, f"{DAY}T19:00:00Z"),
    )
    # The timeline row the ingest's projection writes for it: the triage reads its day's delta from there.
    c.execute(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type)"
        " VALUES (?, 'cm-digest', 'imessage', 'conversation_messages', 'message')",
        (f"{DAY}T19:00:00Z",),
    )
    c.commit()
    # The writer's own path: the day's delta, one item surfaced, the day's objects stored.
    items = [i for i in load_triage_delta(c, f"{DAY}T00:00:00Z", "2026-07-02T00:00:00Z") if i.record_id == "cm-digest"]
    assert [i.title for i in items] == ["msg: " + RAW[:80]], "the triage copies the message text into its title"
    items[0].verdict = "surface"
    _write_signal_objects(c, DAY, items, [], 0.25, [("person:raw-vocab-ottoline", 0.4)], [], {})
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(*, outside_client: bool) -> dict:
    payload = {
        "scope_id": "attention:read",
        "access_mode": "summary",
        "intent": "what did I miss that matters",
        "query": "what did I miss that matters",
        "query_session_id": f"attention-{uuid.uuid4().hex[:8]}",
    }
    if outside_client:
        payload.update(
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
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


def test_the_writer_marks_every_digest_owner_only(conn):
    payloads = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type IN ('attention_summary', 'interest_profile')")]
    assert len(payloads) == 2 and {p.get("disclosure") for p in payloads} == {"owner_only"}


@pytest.mark.asyncio
async def test_an_outside_client_never_reads_an_owner_only_digest(conn):
    payload = await _ask(outside_client=True)
    assert payload.get("turn_outcome") == "live_query", payload
    assert payload.get("disclosure_tier") == "default_disclosure"
    wire = json.dumps(payload, default=str)
    assert "Marrowgate" not in wire and "heronmoss" not in wire, "raw message text reached an outside client"
    assert "raw-vocab-ottoline" not in wire
    # Hiding by absence: no digest, no count of withheld digests, as on a node with no triage.
    assert "Attention digest" not in wire and "Interest profile" not in wire


@pytest.mark.asyncio
async def test_the_owner_still_reads_the_digest(conn):
    payload = await _ask(outside_client=False)
    wire = json.dumps(payload, default=str)
    assert "Marrowgate" in wire and "raw-vocab-ottoline" in wire


def test_the_withheld_count_counts_only_what_the_tier_may_read(conn):
    """The count rides the public narrowing ledger: below the owner's tier it must not count owner-only digests."""
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import _count_attention_summary_items

    manifest = resolve_scope_manifest("attention:read")
    assert _count_attention_summary_items(conn) == 2
    assert _count_attention_summary_items(conn, disclosure_tier="default_disclosure", manifest=manifest) == 0
    assert _count_attention_summary_items(conn, disclosure_tier="default_disclosure", manifest=None) == 0
