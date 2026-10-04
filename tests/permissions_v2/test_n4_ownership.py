"""What counts as the owner's, over the relay (any-to-any N4; A2A-3 §7.4, decision D4; test obligation "Node (N4,
N5)" 5).

protects: the list names every app whose stamped writes landed in an AI-chat, journal or browsing table, and the
older rows a receipt could still cover, each with the module's own statement and preview digest; a first-party
capture app is the owner's without a word (D4) and cannot be declined; "mine" records the existing receipt for exactly
the previewed rows (a stale preview records nothing); "not mine" proves nothing, withdraws what vouched for the app,
and stops the item asking; withdrawing a receipt is the existing revoke, for the whole entry. No reply carries a row.

The node is the journal family's synthetic one (``test_journal_family``): a migrated canonical database with the
journal kind on and one journal source installed for the owner. Every app, word and id is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_journal_family import DATASET, OWNER, RESOURCE, SOURCE, _db, _entry, node  # noqa: F401
from topos.permissions_v2 import ai_chat_capture, capture_receipts, ownership

CAPTURE_APP = "chatgpt-shadow-extension"          # on the node's capture list by default (OD-39)
CAPTURE_SOURCE = ai_chat_capture.OD39_SOURCE_ID
NOTES_APP = "garden-notes-app"                    # an app the owner never vouched for
OTHER_APP = "tide-log-app"


def _chat(path, message_id: str, content: str, *, writer=None, app=None, sender="user", conversation="conv-1",
          ingested="2026-08-01T09:00:00Z"):
    with _db(path) as conn:
        # One parent per conversation (the table has no unique key, and two parents bind nothing).
        if conn.execute("SELECT 1 FROM ai_chat_conversations WHERE conversation_id=?", (conversation,)).fetchone() is None:
            conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, source_id, created_at, "
                         "updated_at) VALUES (?,?,?,?,?)", (conversation, OWNER, CAPTURE_SOURCE, ingested, ingested))
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, source_id, sender_type, content, "
                     "event_at, writer_class, writer_app_id, writer_dataset_id, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (message_id, conversation, CAPTURE_SOURCE, sender, content, "2026-09-10T08:00:00Z", writer, app,
                      DATASET if writer else None, ingested))


@pytest.fixture()
def world(node):
    """Stamped capture prompts and older ones; stamped journal rows of an app nobody vouched for, and older ones."""
    _chat(node, "chat-stamped-1", "Draft a packing list for the weekend.", writer="owner_app", app=CAPTURE_APP,
          ingested="2026-09-02T10:00:00Z")
    _chat(node, "chat-stamped-2", "Suggest a name for the reading group.", writer="owner_app", app=CAPTURE_APP,
          ingested="2026-09-03T10:00:00Z")
    _chat(node, "chat-old-1", "An older prompt about the allotment rota.", ingested="2026-08-20T10:00:00Z")
    _chat(node, "chat-old-2", "An older prompt about bike gears.", ingested="2026-08-21T10:00:00Z")
    _chat(node, "chat-old-reply", "An older assistant reply.", sender="assistant", ingested="2026-08-21T10:00:01Z")
    _entry(node, "j-stamped-1", "Stamped entry about the greenhouse.", writer_class="owner_app", app=NOTES_APP,
           ingested_at="2026-09-05T08:00:00Z")
    _entry(node, "j-stamped-2", "Stamped entry about the compost.", writer_class="owner_app", app=NOTES_APP,
           ingested_at="2026-09-06T08:00:00Z")
    _entry(node, "j-old-1", "Older entry about seed trays.", writer_class=None, dataset=None,
           ingested_at="2026-08-10T08:00:00Z")
    _entry(node, "j-old-2", "Older entry about frost.", writer_class=None, dataset=None,
           ingested_at="2026-08-11T08:00:00Z")
    return node


def _conn(path):
    return sqlite3.connect(str(path))


def listing(path) -> dict:
    with _conn(path) as conn:
        return ownership.listing(conn, owner_id=OWNER, resource_id=RESOURCE)


def act(path, operation, **request):
    conn = _conn(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = getattr(ownership, operation)(conn, owner_id=OWNER, resource_id=RESOURCE, **request)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def app_entry(found, app_id, kind):
    return next(entry for entry in found["apps"] if entry["app_id"] == app_id and entry["kind"] == kind)


def test_the_list_names_each_app_and_each_older_group_with_its_statement_and_digest(world):
    found = listing(world)
    capture = app_entry(found, CAPTURE_APP, "ai_chats")
    assert capture == {"app_id": CAPTURE_APP, "kind": "ai_chats", "items": 2, "state": "yours",
                       "statement": ai_chat_capture.STATEMENT, "preview_digest": None}
    notes = app_entry(found, NOTES_APP, "journal_entries")
    with _conn(world) as conn:
        expected = capture_receipts.preview(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE,
                                            app_id=NOTES_APP)
    assert notes == {"app_id": NOTES_APP, "kind": "journal_entries", "items": 2, "state": "needs_ok",
                     "statement": capture_receipts.FAMILIES["journal_entries"].statement,
                     "preview_digest": expected["preview_digest"]}
    older = {entry["kind"]: entry for entry in found["older"]}
    assert set(older) == {"ai_chats", "journal_entries"}
    assert (older["ai_chats"]["source_id"], older["ai_chats"]["items"], older["ai_chats"]["before"]) == (
        CAPTURE_SOURCE, 2, "2026-09-02")
    assert older["ai_chats"]["statement"] == ai_chat_capture.STATEMENT
    assert (older["journal_entries"]["source_id"], older["journal_entries"]["items"],
            older["journal_entries"]["before"]) == (SOURCE, 2, "2026-09-05")
    assert all(entry["group_id"].startswith("older-") for entry in found["older"])
    text = json.dumps(found)
    for word in ("packing", "greenhouse", "compost", "seed trays", "frost", "allotment", "bike"):
        assert word not in text


def test_mine_records_the_existing_receipt_for_exactly_the_previewed_rows(world):
    notes = app_entry(listing(world), NOTES_APP, "journal_entries")
    answer = act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="mine",
                 preview_digest=notes["preview_digest"])
    assert answer["state"] == "yours" and answer["receipt_id"].startswith("cap-")
    with _conn(world) as conn:
        listed = {row[0] for row in conn.execute("SELECT record_id FROM capture_receipt_rows WHERE receipt_id=?",
                                                 (answer["receipt_id"],))}
        assert listed == {"j-old-1", "j-old-2"}                     # the previewed pre-stamp rows, no more
        assert NOTES_APP in capture_receipts.capture_apps(conn, owner_id=OWNER, table="journal_entries",
                                                         source_id=SOURCE)
    after = listing(world)
    assert app_entry(after, NOTES_APP, "journal_entries")["state"] == "yours"
    assert "journal_entries" not in {entry["kind"] for entry in after["older"]}    # its rows are covered now


def test_a_stale_preview_records_nothing(world):
    notes = app_entry(listing(world), NOTES_APP, "journal_entries")
    _entry(world, "j-old-3", "Another older entry arrives.", writer_class=None, dataset=None)
    with pytest.raises(ownership.Refused) as stale:
        act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="mine",
            preview_digest=notes["preview_digest"])
    assert stale.value.code == "preview_stale"
    with _conn(world) as conn:
        assert not capture_receipts.installed(conn) or conn.execute(
            "SELECT COUNT(*) FROM capture_receipts").fetchone()[0] == 0


def test_not_mine_proves_nothing_withdraws_what_vouched_and_stops_asking(world):
    notes = app_entry(listing(world), NOTES_APP, "journal_entries")
    mine = act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="mine",
               preview_digest=notes["preview_digest"])
    again = app_entry(listing(world), NOTES_APP, "journal_entries")
    answer = act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="not_mine",
                 preview_digest=again["preview_digest"])
    assert answer == {"state": "not_yours", "receipt_id": None}
    with _conn(world) as conn:
        assert NOTES_APP not in capture_receipts.capture_apps(conn, owner_id=OWNER, table="journal_entries",
                                                             source_id=SOURCE)
        revoked = conn.execute("SELECT revoked_at FROM capture_receipts WHERE receipt_id=?",
                               (mine["receipt_id"],)).fetchone()[0]
        assert revoked is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE share_ownership_decisions SET decision='mine'")
    assert app_entry(listing(world), NOTES_APP, "journal_entries")["state"] == "not_yours"
    # A later "mine" is the owner's latest word again.
    latest = app_entry(listing(world), NOTES_APP, "journal_entries")
    assert act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="mine",
               preview_digest=latest["preview_digest"])["state"] == "yours"


def test_withdraw_is_the_existing_revoke_of_the_whole_entry(world):
    notes = app_entry(listing(world), NOTES_APP, "journal_entries")
    mine = act(world, "confirm", item_type="app", item_id=NOTES_APP, decision="mine",
               preview_digest=notes["preview_digest"])
    # A second receipt for the same app on the same table, made through the owner socket's own route.
    _entry(world, "j-old-4", "One more older entry.", writer_class=None, dataset=None)
    with _conn(world) as conn:
        preview = capture_receipts.preview(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE,
                                           app_id=NOTES_APP)
        second = capture_receipts.attest(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE,
                                         app_id=NOTES_APP, preview_digest=preview["preview_digest"], confirm=True)
    assert act(world, "withdraw", receipt_id=mine["receipt_id"]) == {"state": "needs_ok"}
    with _conn(world) as conn:
        live = conn.execute("SELECT COUNT(*) FROM capture_receipts WHERE revoked_at IS NULL").fetchone()[0]
        assert live == 0, second["receipt_id"]
    for receipt_id, code in ((mine["receipt_id"], "receipt_revoked"), ("cap-" + "0" * 32, "receipt_unknown")):
        with pytest.raises(ownership.Refused) as refused:
            act(world, "withdraw", receipt_id=receipt_id)
        assert refused.value.code == code


def test_older_prompts_become_the_owners_through_their_capture_apps_receipt(world):
    group = next(entry for entry in listing(world)["older"] if entry["kind"] == "ai_chats")
    answer = act(world, "confirm", item_type="older", item_id=group["group_id"], decision="mine",
                 preview_digest=group["preview_digest"])
    assert answer["state"] == "yours" and answer["receipt_id"].startswith("aicap-")
    with _conn(world) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM ai_chat_messages WHERE message_id='chat-old-1'").fetchone())
        assert ai_chat_capture.capture_proven(conn, owner_id=OWNER, identity_source_id=CAPTURE_SOURCE, row=row)
        reply = dict(conn.execute("SELECT * FROM ai_chat_messages WHERE message_id='chat-old-reply'").fetchone())
        assert not ai_chat_capture.capture_proven(conn, owner_id=OWNER, identity_source_id=CAPTURE_SOURCE, row=reply)
    assert "ai_chats" not in {entry["kind"] for entry in listing(world)["older"]}
    # Withdrawn, the older group asks again; an older group the owner declines stops asking.
    assert act(world, "withdraw", receipt_id=answer["receipt_id"]) == {"state": "needs_ok"}
    group = next(entry for entry in listing(world)["older"] if entry["kind"] == "ai_chats")
    assert act(world, "confirm", item_type="older", item_id=group["group_id"], decision="not_mine",
               preview_digest=group["preview_digest"]) == {"state": "not_yours", "receipt_id": None}
    assert "ai_chats" not in {entry["kind"] for entry in listing(world)["older"]}


@pytest.mark.parametrize("item_type, item_id, decision, code", [
    ("app", CAPTURE_APP, "not_mine", "item_unknown"),          # a first-party app is the owner's statement (D4)
    ("app", "an-app-nobody-knows", "mine", "item_unknown"),
    ("older", "older-" + "0" * 24, "mine", "item_unknown"),
])
def test_items_that_cannot_be_decided_are_refused(world, item_type, item_id, decision, code):
    with pytest.raises(ownership.Refused) as refused:
        act(world, "confirm", item_type=item_type, item_id=item_id, decision=decision, preview_digest=None)
    assert refused.value.code == code


def test_a_first_party_apps_mine_is_already_true(world):
    assert act(world, "confirm", item_type="app", item_id=CAPTURE_APP, decision="mine",
               preview_digest=None) == {"state": "yours", "receipt_id": None}
