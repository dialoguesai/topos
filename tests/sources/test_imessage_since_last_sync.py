"""The app's "since last" iMessage sync reads only what is newer than the last sync.

On one node the button re-read 39,867 messages that were already stored: the
app sends ``mode="all"`` for "since last", and ``"all"`` resumed from the cursor
of the last UNBOUNDED scan. That cursor was a stopped all-history rescan's, far
below the bounded sync that ran after it, so the next click would have re-read
everything in between again. Since-last now resumes after the newest ROWID any
sync of the dataset has read (the high-water mark), and with no checkpoint it
can trust it stops at a plan, counts and dates only, until the owner confirms.

Every database here is synthetic and lives in ``tmp_path`` or memory; the
reader's default chat.db path is pointed at a file that does not exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import topos.ingestion.local_sync as local_sync
import topos.ingestion.sources.imessage_reader as reader
from topos.ingestion.checkpoints.checkpoint_store import IngestionCheckpoint
from topos.ingestion.checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from topos.ingestion.local_sync import (
    HIGH_WATER_AT_KEY,
    HIGH_WATER_KEY,
    IMESSAGE_SCHEMA_ID,
    OUTCOME_IMPORTED,
    OUTCOME_NEEDS_CONFIRMATION,
    OUTCOME_PREVIEW,
    OUTCOME_UP_TO_DATE,
    UNBOUNDED_CURSOR_KEY,
    describe_imessage_checkpoint,
    run_imessage_sync,
)
from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db, mac_ns

DS = "owner:topos:enrolled"


def _unix(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


OLD = _unix("2024-06-01T12:00:00")
AUG = _unix("2026-08-20T12:00:00")
SEP_27 = _unix("2026-09-27T02:00:00")
SEP_28 = _unix("2026-09-28T09:30:00")
SEP_29 = _unix("2026-09-29T08:15:00")


@pytest.fixture(autouse=True)
def _synthetic_sources_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMESSAGE_CHAT_DB", str(tmp_path / "no-default-chat.db"))
    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kw: None)


def _ids(conn: sqlite3.Connection) -> list[str]:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
    ).fetchone()
    if not found:
        return []
    return sorted(
        (r[0] for r in conn.execute("SELECT message_id FROM conversation_messages")),
        key=lambda mid: int(mid.split(":")[1]),
    )


def _sync(topos: sqlite3.Connection, chat_db: Path, **options) -> dict:
    return run_imessage_sync(
        DS,
        db_conn=topos,
        chat_db_path=chat_db,
        batch_size=options.pop("batch_size", 10),
        sync_options=options,
    )


def _checkpoint(topos: sqlite3.Connection) -> IngestionCheckpoint:
    return SqliteCheckpointStore(topos).get_checkpoint(DS, IMESSAGE_SCHEMA_ID)


def _owner_shaped_chat_db(path: Path) -> None:
    """ROWIDs 1-5 already read; 6 and 7 arrived after the last sync."""
    chat = _make_chat_db(path)
    for rowid, when in ((1, OLD), (2, OLD), (3, OLD), (4, AUG), (5, SEP_27)):
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=f"m{rowid}", date=mac_ns(when))
    _add_message(chat, rowid=6, chat_id=1, handle_id=1, text="new one", date=mac_ns(SEP_28))
    _add_message(chat, rowid=7, chat_id=2, handle_id=2, text="new two", is_from_me=1, date=mac_ns(SEP_29))
    chat.close()


def _save_owner_shaped_checkpoint(topos: sqlite3.Connection) -> None:
    """The live shape: a stopped all-history rescan left the unbounded cursor at 2,
    then a bounded sync read up to 5 and saved that as the last cursor."""
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(
            dataset_id=DS,
            schema_id=IMESSAGE_SCHEMA_ID,
            last_record_id="imessage:5",
            metadata={
                "coverage_recorded": True,
                "exclude_spam": True,
                UNBOUNDED_CURSOR_KEY: "imessage:2",
                "unbounded_exclude_spam": True,
            },
        )
    )


def _spy_cursors(monkeypatch: pytest.MonkeyPatch) -> list:
    real = reader.read_imessage_batch
    cursors: list = []

    def _spy(**kwargs):
        if kwargs.get("rowids") is None:
            cursors.append(kwargs.get("last_rowid"))
        return real(**kwargs)

    monkeypatch.setattr(reader, "read_imessage_batch", _spy)
    return cursors


# --- the fix ------------------------------------------------------------------


def test_since_last_resumes_after_the_last_sync_not_the_rescan_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)
    cursors = _spy_cursors(monkeypatch)

    # Exactly what the app sends for "since last".
    result = _sync(topos, db, mode="all")

    assert result["status"] == "ok", result
    assert result["outcome"] == OUTCOME_IMPORTED
    assert cursors[0] == "imessage:5", "resumed after the last sync, not the rescan's cursor at 2"
    assert _ids(topos) == ["imessage:6", "imessage:7"]
    assert result["records_processed"] == 2
    assert result["plan"]["trusted"] is True
    assert result["plan"]["start_rowid"] == 5
    assert result["plan"]["to_import"] == 2


def test_since_last_by_name_behaves_the_same(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)

    result = _sync(topos, db, mode="since_last")

    assert result["outcome"] == OUTCOME_IMPORTED
    assert _ids(topos) == ["imessage:6", "imessage:7"]


def test_since_last_moves_the_high_water_mark_but_not_the_unproven_coverage(tmp_path: Path) -> None:
    """ROWIDs 3-5 were never read by an unbounded scan here; the cursor must not claim them."""
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)

    _sync(topos, db, mode="since_last")

    metadata = _checkpoint(topos).metadata
    assert metadata[HIGH_WATER_KEY] == 7
    assert metadata[HIGH_WATER_AT_KEY].startswith("2026-09-29")
    assert metadata[UNBOUNDED_CURSOR_KEY] == "imessage:2"


def test_a_stopped_rescan_saving_a_lower_cursor_does_not_pull_since_last_back(tmp_path: Path) -> None:
    """The last batch cursor can fall below what was read; the high-water mark cannot."""
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(
            dataset_id=DS,
            schema_id=IMESSAGE_SCHEMA_ID,
            last_record_id="imessage:2",
            metadata={"coverage_recorded": True, "exclude_spam": True, HIGH_WATER_KEY: 5,
                      UNBOUNDED_CURSOR_KEY: "imessage:2", "unbounded_exclude_spam": True},
        )
    )

    result = _sync(topos, db, mode="since_last")

    assert result["plan"]["start_rowid"] == 5
    assert _ids(topos) == ["imessage:6", "imessage:7"]


def test_a_checkpoint_from_before_coverage_was_recorded_still_resumes_since_last(tmp_path: Path) -> None:
    """A legacy cursor says where the last sync stopped, whatever window it had."""
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(dataset_id=DS, schema_id=IMESSAGE_SCHEMA_ID, last_record_id="imessage:5", metadata={})
    )

    result = _sync(topos, db, mode="all")

    assert result["plan"]["trusted"] is True
    assert _ids(topos) == ["imessage:6", "imessage:7"]


def test_nothing_new_is_up_to_date_and_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)
    _sync(topos, db, mode="since_last")

    again = _sync(topos, db, mode="since_last")

    assert again["outcome"] == OUTCOME_UP_TO_DATE
    assert again["records_processed"] == 0
    assert again["plan"]["to_import"] == 0
    assert _ids(topos) == ["imessage:6", "imessage:7"]


# --- no checkpoint to trust: a plan, then a confirmation ----------------------


def test_with_no_checkpoint_since_last_writes_nothing_and_returns_a_plan(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="old", date=mac_ns(OLD))
    _add_message(chat, rowid=2, chat_id=2, handle_id=2, text="junk", is_spam=1, date=mac_ns(AUG))
    _add_message(chat, rowid=3, chat_id=1, handle_id=1, text="recent", date=mac_ns(SEP_28))
    chat.close()
    topos = sqlite3.connect(":memory:")

    result = _sync(topos, db, mode="since_last")

    assert result["status"] == "ok"
    assert result["outcome"] == OUTCOME_NEEDS_CONFIRMATION
    plan = result["plan"]
    assert (plan["trusted"], plan["reason"], plan["start_rowid"]) == (False, "no_checkpoint", 0)
    assert (plan["messages"], plan["spam_skipped"], plan["to_import"]) == (3, 1, 2)
    assert plan["first_at"].startswith("2024-06-01")
    assert plan["last_at"].startswith("2026-09-28")
    assert _ids(topos) == []
    assert _checkpoint(topos) is None, "a plan saves no checkpoint"


def test_confirming_the_plan_runs_it(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="old", date=mac_ns(OLD))
    _add_message(chat, rowid=2, chat_id=1, handle_id=1, text="recent", date=mac_ns(SEP_28))
    chat.close()
    topos = sqlite3.connect(":memory:")
    plan = _sync(topos, db, mode="since_last")["plan"]

    result = _sync(topos, db, mode="since_last", confirm_start_rowid=plan["start_rowid"])

    assert result["outcome"] == OUTCOME_IMPORTED
    assert _ids(topos) == ["imessage:1", "imessage:2"]
    metadata = _checkpoint(topos).metadata
    assert metadata[HIGH_WATER_KEY] == 2
    # Read from ROWID 0, so it also proves all-history coverage.
    assert metadata[UNBOUNDED_CURSOR_KEY] == "imessage:2"


def test_a_confirmation_for_another_start_is_not_a_confirmation(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="old", date=mac_ns(OLD))
    chat.close()
    topos = sqlite3.connect(":memory:")

    for stale in (5, "5", True, None, "zero"):
        result = _sync(topos, db, mode="since_last", confirm_start_rowid=stale)
        assert result["outcome"] == OUTCOME_NEEDS_CONFIRMATION, stale
    assert _ids(topos) == []


def test_a_checkpoint_chat_db_no_longer_reaches_needs_confirmation(tmp_path: Path) -> None:
    """chat.db replaced or reset: resuming past its end would read nothing, silently, forever."""
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    _add_message(chat, rowid=1, chat_id=1, handle_id=1, text="after reset", date=mac_ns(SEP_28))
    chat.close()
    topos = sqlite3.connect(":memory:")
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(dataset_id=DS, schema_id=IMESSAGE_SCHEMA_ID, last_record_id="imessage:500",
                            metadata={"coverage_recorded": True, HIGH_WATER_KEY: 500})
    )

    result = _sync(topos, db, mode="since_last")

    assert result["outcome"] == OUTCOME_NEEDS_CONFIRMATION
    assert result["plan"]["reason"] == "checkpoint_beyond_chat_db"
    assert result["plan"]["start_rowid"] == 0
    assert result["plan"]["chat_db_max_rowid"] == 1
    assert _ids(topos) == []


def test_dry_run_returns_the_plan_and_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)
    before = _checkpoint(topos)

    result = _sync(topos, db, mode="since_last", dry_run=True)

    assert result["outcome"] == OUTCOME_PREVIEW
    assert result["plan"]["to_import"] == 2
    assert result["plan"]["first_at"].startswith("2026-09-28")
    assert _ids(topos) == []
    assert _checkpoint(topos) == before


def test_a_dry_run_is_never_a_real_run_in_another_mode(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")

    for mode in ("full_history", "3m", "custom"):
        result = _sync(topos, db, mode=mode, start_date="2020-01-01", dry_run=True)
        assert result["status"] == "error", mode
    assert _ids(topos) == []


def test_the_plan_counts_rows_another_dataset_already_holds(tmp_path: Path) -> None:
    """Ids are global: a row stored under another dataset is not imported again."""
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)
    run_imessage_sync(
        "owner:default", db_conn=topos, chat_db_path=db, batch_size=10,
        sync_options={"mode": "custom", "start_date": "2026-09-28"},
    )
    assert _ids(topos) == ["imessage:6", "imessage:7"]

    plan = _sync(topos, db, mode="since_last", dry_run=True)["plan"]

    assert (plan["messages"], plan["already_stored"], plan["to_import"]) == (2, 2, 0)
    # The dates describe what would be imported: nothing, so no dates.
    assert (plan["first_at"], plan["last_at"]) == (None, None)


# --- the modes that still rescan ------------------------------------------------


def test_full_history_still_reads_the_gap_since_last_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    _save_owner_shaped_checkpoint(topos)
    cursors = _spy_cursors(monkeypatch)

    result = _sync(topos, db, mode="full_history")

    assert result["status"] == "ok"
    assert cursors[0] == "imessage:2", "full history resumes at the unbounded cursor, as 'all' used to"
    assert _ids(topos) == ["imessage:3", "imessage:4", "imessage:5", "imessage:6", "imessage:7"]
    metadata = _checkpoint(topos).metadata
    assert metadata[UNBOUNDED_CURSOR_KEY] == "imessage:7"
    assert metadata[HIGH_WATER_KEY] == 7


def test_a_bounded_sync_raises_the_high_water_mark_too(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")

    _sync(topos, db, mode="custom", start_date="2026-09-01")

    assert _ids(topos) == ["imessage:5", "imessage:6", "imessage:7"]
    metadata = _checkpoint(topos).metadata
    assert metadata[HIGH_WATER_KEY] == 7
    assert UNBOUNDED_CURSOR_KEY not in metadata
    # And since-last after it reads nothing older.
    assert _sync(topos, db, mode="since_last")["outcome"] == OUTCOME_UP_TO_DATE


# --- pacing ---------------------------------------------------------------------


def test_a_paced_run_pauses_between_batches_and_never_inside_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "chat.db"
    chat = _make_chat_db(db)
    for rowid in range(1, 6):
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=f"m{rowid}", date=mac_ns(SEP_28))
    chat.close()
    topos = sqlite3.connect(":memory:")
    plan = _sync(topos, db, mode="since_last")["plan"]
    saved_before_sleep: list = []

    def _sleep(seconds: float) -> None:
        saved_before_sleep.append((seconds, _checkpoint(topos).last_record_id))

    monkeypatch.setattr(local_sync.time, "sleep", _sleep)
    result = _sync(topos, db, mode="since_last", confirm_start_rowid=plan["start_rowid"],
                   batch_size=2, pause_seconds=1.5)

    assert result["records_processed"] == 5
    # Batches of 2, 2, 1: a pause after each full batch, each after its checkpoint.
    assert saved_before_sleep == [(1.5, "imessage:2"), (1.5, "imessage:4")]


# --- what the settings screen shows ---------------------------------------------


def test_describe_checkpoint_says_where_the_next_since_last_sync_starts(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _owner_shaped_chat_db(db)
    topos = sqlite3.connect(":memory:")
    # A legacy-shaped checkpoint whose ROWID is stored: its time comes from the row.
    _sync(topos, db, mode="custom", start_date="2026-09-27")
    SqliteCheckpointStore(topos).save_checkpoint(
        IngestionCheckpoint(dataset_id=DS, schema_id=IMESSAGE_SCHEMA_ID, last_record_id="imessage:7",
                            metadata={UNBOUNDED_CURSOR_KEY: "imessage:2", "held_rowids": {"4": "empty_body"}})
    )

    described = describe_imessage_checkpoint(topos, DS)

    assert described["has_checkpoint"] is True
    assert described["high_water_rowid"] == 7
    assert described["unbounded_rowid"] == 2
    assert described["held"] == 1
    assert str(described["high_water_at"]).startswith("2026-09-29")
    assert describe_imessage_checkpoint(topos, "no:such:dataset")["has_checkpoint"] is False
    assert describe_imessage_checkpoint(sqlite3.connect(":memory:"), DS)["has_checkpoint"] is False


def test_the_enrollment_contract_constant_is_the_permissions_one() -> None:
    from topos.permissions_v2.ingest_protocol import IMESSAGE_READER_CONTRACT

    assert local_sync.IMESSAGE_ENROLLMENT_CONTRACT == IMESSAGE_READER_CONTRACT
