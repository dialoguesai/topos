"""iMessage sync must skip Apple-filtered spam so it never enters the node DB.

chat.is_filtered = 2 is the Unknown Senders inbox. message.is_spam = 1 is
Apple's junk flag. Both are omitted by default; the owner can turn the skip
off. Checkpoint advances past skipped ROWIDs so an all-spam page cannot stall
the next sync.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.ingestion.local_sync import run_imessage_sync
from topos.ingestion.sources.imessage_reader import (
    read_imessage_batch,
    row_is_imessage_spam,
)
from topos.storage.canonical.conversations_tables import CONVERSATION_MESSAGES_TABLE

MAC_DATE = 700_000_000


def _make_chat_db(path: Path, *, include_spam_columns: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    chat_filtered = "is_filtered INTEGER DEFAULT 0," if include_spam_columns else ""
    message_spam = "is_spam INTEGER DEFAULT 0," if include_spam_columns else ""
    conn.executescript(
        f"""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            chat_identifier TEXT,
            {chat_filtered}
            display_name TEXT
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            subject TEXT,
            attributedBody BLOB,
            associated_message_guid TEXT,
            associated_message_type INTEGER,
            cache_has_attachments INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0,
            {message_spam}
            date INTEGER,
            handle_id INTEGER,
            is_from_me INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15555550100')")
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (2, '+15555550999')")
    conn.commit()
    return conn


def _add_message(
    conn: sqlite3.Connection,
    *,
    rowid: int,
    chat_id: int,
    handle_id: int,
    text: str,
    is_filtered: int = 0,
    is_spam: int = 0,
    chat_identifier: str | None = None,
) -> None:
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
    filtered_sql = "is_filtered, " if "is_filtered" in cols else ""
    filtered_val = f"{int(is_filtered)}, " if "is_filtered" in cols else ""
    conn.execute(
        f"""
        INSERT OR IGNORE INTO chat (ROWID, guid, chat_identifier, {filtered_sql} display_name)
        VALUES (?, ?, ?, {filtered_val} NULL)
        """,
        (chat_id, f"iMessage;-;chat{chat_id}", chat_identifier or f"+15555550{chat_id:03d}"),
    )
    if "is_filtered" in cols:
        conn.execute("UPDATE chat SET is_filtered = ? WHERE ROWID = ?", (int(is_filtered), chat_id))
    msg_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(message)").fetchall()}
    spam_sql = "is_spam, " if "is_spam" in msg_cols else ""
    spam_val = f"{int(is_spam)}, " if "is_spam" in msg_cols else ""
    conn.execute(
        f"""
        INSERT INTO message (
            ROWID, text, subject, attributedBody, associated_message_guid,
            associated_message_type, cache_has_attachments, item_type,
            {spam_sql} date, handle_id, is_from_me
        ) VALUES (?, ?, NULL, NULL, NULL, 0, 0, 0, {spam_val} ?, ?, 0)
        """,
        (rowid, text, MAC_DATE, handle_id),
    )
    conn.execute(
        "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
        (chat_id, rowid),
    )
    conn.commit()


def test_row_is_imessage_spam_unknown_sender_and_junk_flag():
    assert row_is_imessage_spam({"is_filtered": 2, "is_spam": 0}) is True
    assert row_is_imessage_spam({"is_filtered": 0, "is_spam": 1}) is True
    assert row_is_imessage_spam({"is_filtered": 0, "is_spam": 0}) is False
    assert row_is_imessage_spam({"is_filtered": 1, "is_spam": 0}) is False
    assert row_is_imessage_spam({}) is False


def test_reader_skips_unknown_senders_and_junk(tmp_path: Path):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    _add_message(conn, rowid=1, chat_id=1, handle_id=1, text="from a friend", is_filtered=0)
    _add_message(conn, rowid=2, chat_id=2, handle_id=2, text="Coinbase verify now", is_filtered=2)
    _add_message(conn, rowid=3, chat_id=3, handle_id=2, text="Free iPhone", is_spam=1)
    conn.close()

    batch = read_imessage_batch(chat_db_path=db, exclude_spam=True)
    assert [row["content"] for row in batch.rows] == ["from a friend"]
    assert batch.records_skipped == 2
    assert batch.scanned_count == 3
    assert batch.max_scanned_rowid == 3


def test_reader_includes_spam_when_exclude_spam_is_false(tmp_path: Path):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    _add_message(conn, rowid=1, chat_id=2, handle_id=2, text="Coinbase verify now", is_filtered=2)
    conn.close()

    batch = read_imessage_batch(chat_db_path=db, exclude_spam=False)
    assert [row["content"] for row in batch.rows] == ["Coinbase verify now"]
    assert batch.records_skipped == 0


def test_reader_without_spam_columns_keeps_everything(tmp_path: Path):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db, include_spam_columns=False)
    _add_message(conn, rowid=1, chat_id=1, handle_id=2, text="maybe spam, no flag")
    conn.close()

    batch = read_imessage_batch(chat_db_path=db, exclude_spam=True)
    assert [row["content"] for row in batch.rows] == ["maybe spam, no flag"]
    assert batch.records_skipped == 0


def test_all_spam_batch_still_reports_scanned_rowid(tmp_path: Path):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    _add_message(conn, rowid=10, chat_id=2, handle_id=2, text="junk a", is_filtered=2)
    _add_message(conn, rowid=11, chat_id=2, handle_id=2, text="junk b", is_filtered=2)
    conn.close()

    batch = read_imessage_batch(chat_db_path=db, batch_size=2, exclude_spam=True)
    assert batch.rows == []
    assert batch.records_skipped == 2
    assert batch.scanned_count == 2
    assert batch.max_scanned_rowid == 11


def test_sync_does_not_write_spam_and_advances_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    _add_message(conn, rowid=1, chat_id=1, handle_id=1, text="real thread")
    _add_message(conn, rowid=2, chat_id=2, handle_id=2, text="unknown sender spam", is_filtered=2)
    _add_message(conn, rowid=3, chat_id=3, handle_id=2, text="apple junk flag", is_spam=1)
    conn.close()

    import topos.ingestion.local_sync as local_sync

    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kwargs: None)

    topos_conn = sqlite3.connect(":memory:")
    result = run_imessage_sync(
        "owner:default",
        db_conn=topos_conn,
        chat_db_path=db,
        batch_size=10,
        sync_options={"mode": "all"},
    )
    assert result["status"] == "ok"
    assert result["records_processed"] == 1
    assert result["records_skipped"] == 2
    assert result["exclude_spam"] is True
    assert result["last_record_id"] == "imessage:3"

    contents = [
        row[0]
        for row in topos_conn.execute(
            f"SELECT content FROM {CONVERSATION_MESSAGES_TABLE} ORDER BY message_id"
        ).fetchall()
    ]
    assert contents == ["real thread"]


def test_sync_honors_exclude_spam_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "chat.db"
    conn = _make_chat_db(db)
    _add_message(conn, rowid=1, chat_id=2, handle_id=2, text="unknown sender spam", is_filtered=2)
    conn.close()

    import topos.ingestion.local_sync as local_sync

    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kwargs: None)

    topos_conn = sqlite3.connect(":memory:")
    result = run_imessage_sync(
        "owner:default",
        db_conn=topos_conn,
        chat_db_path=db,
        sync_options={"mode": "all", "exclude_spam": False},
    )
    assert result["status"] == "ok"
    assert result["records_processed"] == 1
    assert result["records_skipped"] == 0
    contents = [
        row[0]
        for row in topos_conn.execute(
            f"SELECT content FROM {CONVERSATION_MESSAGES_TABLE}"
        ).fetchall()
    ]
    assert contents == ["unknown sender spam"]
