"""The legacy iMessage/Signal local sync must not lose rows behind its checkpoint.

A local sync is incremental: whatever sits behind the saved cursor is never read
again. So every way the cursor can move past a row that was not written is
permanent data loss, and every one of these was reachable:

- a row that fails validation (a NULL ``message.date``) or has no body the reader
  can build was dropped, uncounted, and the cursor moved past it;
- a bounded sync (``mode="3m"``, the default at both doors) saved its cursor, and
  a later ``mode="all"`` resumed from it, so older history was never read;
- Signal paged on ``sent_at > cursor``, so a row sharing the boundary timestamp
  with the last row of a full batch was skipped;
- the reader copied only ``chat.db``, so messages still in ``chat.db-wal`` were
  invisible, and the cursor later moved past them.

Every database here is synthetic and lives in ``tmp_path``. The reader paths are
redirected there too, so nothing reads the owner's chat.db, Signal store or node
database.
"""

from __future__ import annotations

import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

import topos.ingestion.local_sync as local_sync
from topos.ingestion.checkpoints.checkpoint_store import IngestionCheckpoint
from topos.ingestion.checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from topos.ingestion.local_sync import (
    IMESSAGE_SCHEMA_ID,
    SIGNAL_SCHEMA_ID,
    UNBOUNDED_INHERITED_KEY,
    run_imessage_sync,
    run_signal_sync,
)
from topos.ingestion.sources.imessage_reader import read_imessage_batch
from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db, mac_ns

DS = "owner:default"
DAY = 86_400
NOW = time.time()


@pytest.fixture(autouse=True)
def _synthetic_sources_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every default reader path at tmp_path, and skip enrichment."""
    monkeypatch.setenv("IMESSAGE_CHAT_DB", str(tmp_path / "no-default-chat.db"))
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "no-default-signal.sqlite"))
    monkeypatch.setenv("SIGNAL_CONFIG_PATH", str(tmp_path / "no-default-signal-config.json"))
    monkeypatch.delenv("SIGNAL_KEY_HEX", raising=False)
    monkeypatch.delenv("SIGNAL_SQLCIPHER_KEY", raising=False)
    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kw: None)


def _message_ids(conn: sqlite3.Connection) -> list[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversation_messages'"
    ).fetchone()
    if not exists:
        return []
    return sorted(r[0] for r in conn.execute("SELECT message_id FROM conversation_messages"))


def _sync_imessage(topos: sqlite3.Connection, chat_db: Path, **options) -> dict:
    result = run_imessage_sync(
        DS,
        db_conn=topos,
        chat_db_path=chat_db,
        batch_size=options.pop("batch_size", 10),
        sync_options=options,
    )
    assert result["status"] == "ok", result
    return result


# --- checkpoint moves past unwritten iMessage rows -------------------------


def test_a_row_with_no_date_is_counted_and_written_once_it_has_one(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="no date yet", date=None)
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="dated", date=mac_ns(NOW - DAY))
    chat.close()

    topos = sqlite3.connect(":memory:")
    first = _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:2"]
    assert first["records_processed"] == 1
    assert first["records_held"] == 1, "an unwritten row must be counted, not vanish"

    chat = sqlite3.connect(str(db))
    chat.execute("UPDATE message SET date = ? WHERE ROWID = 1", (mac_ns(NOW - 2 * DAY),))
    chat.commit()
    chat.close()

    second = _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:1", "imessage:2"], (
        "the cursor already passed ROWID 1; it must still be written once it is valid"
    )
    assert second["records_held"] == 0


def test_a_row_with_no_readable_body_is_counted_and_retried(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text=None, date=mac_ns(NOW - DAY))
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="hello", date=mac_ns(NOW - DAY))
    _add_message(chat, rowid=3, chat_id=2, handle_id=2, text="junk", is_spam=1, date=mac_ns(NOW - DAY))
    chat.close()

    topos = sqlite3.connect(":memory:")
    first = _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:2"]
    # Every scanned row is accounted for exactly once.
    assert (first["records_processed"], first["records_skipped"], first["records_held"]) == (1, 1, 1)

    chat = sqlite3.connect(str(db))
    chat.execute("UPDATE message SET text = 'readable now' WHERE ROWID = 1")
    chat.commit()
    chat.close()

    second = _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:1", "imessage:2"]
    assert second["records_held"] == 0
    content = topos.execute(
        "SELECT content FROM conversation_messages WHERE message_id = 'imessage:1'"
    ).fetchone()[0]
    assert content == "readable now"


def test_a_held_row_that_left_chat_db_is_dropped_from_the_retry_list(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text=None, date=mac_ns(NOW - DAY))
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="hello", date=mac_ns(NOW - DAY))
    chat.close()

    topos = sqlite3.connect(":memory:")
    assert _sync_imessage(topos, db, mode="all")["records_held"] == 1

    chat = sqlite3.connect(str(db))
    chat.execute("DELETE FROM message WHERE ROWID = 1")
    chat.execute("DELETE FROM chat_message_join WHERE message_id = 1")
    chat.commit()
    chat.close()

    assert _sync_imessage(topos, db, mode="all")["records_held"] == 0
    checkpoint = SqliteCheckpointStore(topos).get_checkpoint(DS, IMESSAGE_SCHEMA_ID)
    assert not (checkpoint.metadata or {}).get("held_rowids")


# --- a bounded sync must not hide older history ------------------------------


def test_an_all_sync_after_a_bounded_sync_still_reads_older_history(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="a year ago", date=mac_ns(NOW - 365 * DAY))
    _add_message(
        chat, rowid=2, chat_id=2, handle_id=0, text="last week", is_from_me=1, date=mac_ns(NOW - 7 * DAY)
    )
    chat.close()

    topos = sqlite3.connect(":memory:")
    _sync_imessage(topos, db, mode="3m")
    assert _message_ids(topos) == ["imessage:2"]

    _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:1", "imessage:2"]


def test_a_checkpoint_that_does_not_say_what_it_covered_is_not_resumed_by_all(tmp_path: Path) -> None:
    """A legacy iMessage checkpoint most likely came from a 3m sync: both doors default to it.

    Signal's legacy checkpoints are trusted instead; see the Signal test below.
    """
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="old", date=mac_ns(NOW - 365 * DAY))
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="new", date=mac_ns(NOW - DAY))
    chat.close()

    topos = sqlite3.connect(":memory:")
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(
            dataset_id=DS,
            schema_id=IMESSAGE_SCHEMA_ID,
            last_record_id="imessage:2",
            metadata={"exclude_spam": True},
        )
    )
    _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:1", "imessage:2"]


def test_a_repeat_all_sync_resumes_from_its_own_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The coverage rule must not degrade an incremental sync into a full rescan."""
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="one", date=mac_ns(NOW - DAY))
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="two", date=mac_ns(NOW - DAY))
    chat.close()

    topos = sqlite3.connect(":memory:")
    _sync_imessage(topos, db, mode="all")
    # A bounded sync in between must not cost the unbounded cursor either.
    _sync_imessage(topos, db, mode="3m")

    import topos.ingestion.sources.imessage_reader as reader

    real = reader.read_imessage_batch
    cursors: list = []

    def _spy(**kwargs):
        cursors.append(kwargs.get("last_rowid"))
        return real(**kwargs)

    monkeypatch.setattr(reader, "read_imessage_batch", _spy)
    _sync_imessage(topos, db, mode="all")
    assert cursors and cursors[0] == "imessage:2"


def test_turning_spam_exclusion_off_rereads_rows_an_earlier_sync_skipped(tmp_path: Path) -> None:
    """Same rule, second axis: a cursor only vouches for the policy it ran under."""
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=2, handle_id=2, text="unknown sender", is_filtered=2)
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="friend")
    chat.close()

    topos = sqlite3.connect(":memory:")
    _sync_imessage(topos, db, mode="all")
    assert _message_ids(topos) == ["imessage:2"]

    _sync_imessage(topos, db, mode="all", exclude_spam=False)
    assert _message_ids(topos) == ["imessage:1", "imessage:2"]


# --- WAL-only messages --------------------------------------------------------


def test_reader_sees_messages_still_only_in_the_wal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    private_tmp = tmp_path / "reader-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(private_tmp))

    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    assert chat.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    chat.execute("PRAGMA wal_autocheckpoint=0")
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="committed, not checkpointed")
    assert (tmp_path / "chat.db-wal").stat().st_size > 0
    try:
        batch = read_imessage_batch(chat_db_path=db)
    finally:
        chat.close()

    assert [row["content"] for row in batch.rows] == ["committed, not checkpointed"]
    assert list(private_tmp.iterdir()) == [], "the private copy and its sidecars must be removed"


# --- Signal -------------------------------------------------------------------


@pytest.fixture
def signal_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A plaintext Signal-shaped database read through a stand-in for pysqlcipher3.

    The cipher PRAGMAs are unknown to plain SQLite, which ignores them.
    """
    fake = types.ModuleType("pysqlcipher3")
    fake.dbapi2 = sqlite3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pysqlcipher3", fake)
    monkeypatch.setitem(sys.modules, "pysqlcipher3.dbapi2", sqlite3)
    path = tmp_path / "signal.sqlite"
    monkeypatch.setenv("SIGNAL_DB_PATH", str(path))
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            body TEXT,
            sent_at INTEGER,
            type TEXT,
            conversationId TEXT,
            sourceServiceId TEXT
        )
        """
    )
    conn.commit()
    return conn


def _add_signal(conn: sqlite3.Connection, msg_id: str, sent_at_ms: int, body: str) -> None:
    conn.execute(
        "INSERT INTO messages (id, body, sent_at, type, conversationId, sourceServiceId) "
        "VALUES (?, ?, ?, 'incoming', 'conv-1', 'svc-peer')",
        (msg_id, body, sent_at_ms),
    )
    conn.commit()


def _sync_signal(topos: sqlite3.Connection, **options) -> dict:
    result = run_signal_sync(
        DS,
        db_conn=topos,
        my_phone_number="+15555550100",
        owner_user_id=DS,
        batch_size=options.pop("batch_size", 10),
        sync_options={"signal_hex_key": "00", **options},
    )
    assert result["status"] == "ok", result
    return result


def test_signal_sync_keeps_rows_tied_across_a_batch_boundary(signal_db: sqlite3.Connection) -> None:
    t1 = int((NOW - 2 * DAY) * 1000)
    t2 = t1 + 60_000
    _add_signal(signal_db, "msg-a", t1, "first")
    _add_signal(signal_db, "msg-b", t2, "same millisecond")
    _add_signal(signal_db, "msg-c", t2, "same millisecond too")

    topos = sqlite3.connect(":memory:")
    result = _sync_signal(topos, mode="all", batch_size=2)
    assert len(_message_ids(topos)) == 3
    assert result["records_processed"] == 3


def test_signal_all_sync_after_a_bounded_sync_still_reads_older_history(signal_db: sqlite3.Connection) -> None:
    _add_signal(signal_db, "msg-old", int((NOW - 400 * DAY) * 1000), "old")
    _add_signal(signal_db, "msg-new", int((NOW - 7 * DAY) * 1000), "new")

    topos = sqlite3.connect(":memory:")
    _sync_signal(topos, mode="3m")
    assert len(_message_ids(topos)) == 1

    _sync_signal(topos, mode="all")
    assert len(_message_ids(topos)) == 2


def test_a_large_hold_list_is_retried_from_one_chat_db_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying held rows must not cost one multi-GB chat.db copy per chunk of ROWIDs."""
    import topos.ingestion.sources.imessage_reader as reader

    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    for rowid in range(1, 1201):
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=None)
    chat.close()

    topos = sqlite3.connect(":memory:")
    assert _sync_imessage(topos, db, mode="all", batch_size=5000)["records_held"] == 1200

    chat = sqlite3.connect(str(db))
    chat.execute("UPDATE message SET text = 'readable now'")
    chat.commit()
    chat.close()

    snapshots: list = []
    real = reader._snapshot_chat_db
    monkeypatch.setattr(reader, "_snapshot_chat_db", lambda *a: (snapshots.append(a), real(*a))[1])
    second = _sync_imessage(topos, db, mode="all", batch_size=5000)

    assert second["records_held"] == 0
    assert len(_message_ids(topos)) == 1200
    assert len(snapshots) == 2, "one snapshot for the retry pass, one for the incremental scan"


def test_signal_trusts_a_checkpoint_from_before_coverage_was_recorded(signal_db: sqlite3.Connection) -> None:
    """With no options Signal syncs everything, so its legacy cursor is resumed, not rescanned.

    The adopted cursor stays marked as inherited, through a bounded sync in
    between, so it is never recorded as coverage a sync proved.
    """
    old_ms = int((NOW - 400 * DAY) * 1000)
    cursor_ms = int((NOW - 200 * DAY) * 1000)
    _add_signal(signal_db, "msg-before-cursor", old_ms, "already synced before the upgrade")
    _add_signal(signal_db, "msg-at-cursor", cursor_ms, "last row the legacy sync read")
    _add_signal(signal_db, "msg-recent", int((NOW - 7 * DAY) * 1000), "new")

    topos = sqlite3.connect(":memory:")
    store = SqliteCheckpointStore(topos)
    store.save_checkpoint(
        IngestionCheckpoint(
            dataset_id=DS,
            schema_id=SIGNAL_SCHEMA_ID,
            last_record_id=f"signal:0:{cursor_ms / 1000.0:.6f}",
            metadata={},
        )
    )

    _sync_signal(topos, mode="3m")
    _sync_signal(topos, mode="all")

    written = {r[0].split(":")[1] for r in topos.execute("SELECT message_id FROM conversation_messages")}
    assert written == {"msg-at-cursor", "msg-recent"}, "resumed at the legacy cursor, re-reading its millisecond"
    assert store.get_checkpoint(DS, SIGNAL_SCHEMA_ID).metadata.get(UNBOUNDED_INHERITED_KEY) is True
