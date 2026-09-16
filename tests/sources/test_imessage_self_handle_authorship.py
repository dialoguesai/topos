"""A correspondent whose handle is spelled 'Self' never becomes the owner.

The legacy reader writes the owner's messages as sender_id 'self', and the sync
derived authorship back from that sender id, so a handle named 'Self' was stored
as the owner and its words minted owner facts. The sync now stores chat.db's
is_from_me, and the reader namespaces a handle spelled 'self'.

The namespacing is what closes the path. The role gate still counts
sender_id 'self' as the owner even beside an explicit is_from_self 0, because
rows written before that column existed, and rows re-staged from raw, store the
owner's own messages exactly that way.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.features.facts.extract import extract_facts_from_batch
from topos.ingestion.local_sync import run_imessage_sync
from topos.storage.db.migrations import apply_all_migrations

from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db

STATEMENT = "I work at Ferrograph Instruments"
SELF_HANDLE = 3


def _chat_db(tmp_path: Path, *, is_from_me: int) -> Path:
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, 'Self')", (SELF_HANDLE,))
    _add_message(conn, rowid=1, chat_id=1, handle_id=SELF_HANDLE, text=STATEMENT)
    if is_from_me:
        conn.execute("UPDATE message SET is_from_me = 1, handle_id = 0 WHERE ROWID = 1")
    conn.commit()
    conn.close()
    return db


def _sync_and_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, is_from_me: int):
    import topos.ingestion.local_sync as local_sync

    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kwargs: None)
    node = sqlite3.connect(str(tmp_path / "node.db"))
    apply_all_migrations(node)
    result = run_imessage_sync(
        "owner:default",
        db_conn=node,
        chat_db_path=_chat_db(tmp_path, is_from_me=is_from_me),
        sync_options={"mode": "all"},
    )
    assert result["status"] == "ok" and result["records_processed"] == 1
    node.row_factory = sqlite3.Row
    rows = [
        {**dict(row), "_table": "conversation_messages"}
        for row in node.execute("SELECT * FROM conversation_messages")
    ]
    node.row_factory = None
    extract_facts_from_batch(node, rows)
    works_at = [
        payload
        for (raw,) in node.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact'")
        if (payload := json.loads(raw)).get("predicate") == "works_at"
    ]
    contacts = node.execute("SELECT is_self FROM contacts").fetchall()
    return rows, works_at, contacts


def test_incoming_message_from_a_handle_named_self_is_not_the_owner(tmp_path, monkeypatch):
    rows, works_at, contacts = _sync_and_extract(tmp_path, monkeypatch, is_from_me=0)

    (row,) = rows
    assert row["is_from_self"] == 0
    assert row["sender_id"].casefold() != "self"
    assert works_at == []
    assert contacts == [(0,)]


def test_sync_stores_the_native_flag_even_if_the_sender_id_reads_self(tmp_path, monkeypatch):
    # A reader that forgot to namespace the handle: the sync still stores
    # is_from_me, not an owner flag re-derived from the sender id.
    from topos.ingestion.sources import imessage_reader

    monkeypatch.setattr(imessage_reader, "_normalize_sender_id", lambda value: str(value).strip() or None)
    rows, _works_at, _contacts = _sync_and_extract(tmp_path, monkeypatch, is_from_me=0)

    (row,) = rows
    assert (row["is_from_self"], row["sender_id"]) == (0, "Self")


@pytest.mark.parametrize("flag", [0, False, None])
def test_an_explicit_zero_never_demotes_a_row_the_owner_wrote_as_self(flag):
    """Legacy and re-staged owner rows are (0, 'self'): the flag's absence of 1 is not evidence."""
    from topos.features.provenance.roles import owner_authored

    row = {"message_id": "m1", "sender_id": "self", "is_from_self": flag, "content": STATEMENT}
    assert owner_authored(row, table="conversation_messages") is True


@pytest.mark.parametrize("flag,owner", [(1, True), (True, True), ("1", False), ("true", False), (1.0, False)])
def test_only_a_typed_flag_marks_a_row_with_another_sender_as_the_owners(flag, owner):
    from topos.features.provenance.roles import owner_authored

    row = {"message_id": "m1", "sender_id": "+15555550123", "is_from_self": flag, "content": STATEMENT}
    assert owner_authored(row, table="conversation_messages") is owner


def test_the_same_statement_sent_by_the_owner_is_one_owner_fact(tmp_path, monkeypatch):
    rows, works_at, _contacts = _sync_and_extract(tmp_path, monkeypatch, is_from_me=1)

    (row,) = rows
    assert (row["is_from_self"], row["sender_id"]) == (1, "self")
    assert [(fact["object_value"], fact["asserted_by"]) for fact in works_at] == [
        ("Ferrograph Instruments", "owner")
    ]
