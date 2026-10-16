"""Guards for the Cursor reader (lane L6, CONTRACTS.md §2.2–2.4, §2.7, §2.7b; spike A P0.12).

Extraction only: ``common`` is the stub from ``tests/fixtures/ai_coding_tools``,
the cursor store is in-memory, and every root is synthetic under ``tmp_path``:
a WAL-mode ``state.vscdb`` built from ``state_vscdb_seed.json`` plus one copied
``agent-transcripts`` JSONL. Nothing here opens the real ``~/.cursor`` or
Cursor's live ``state.vscdb``.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

from tests.fixtures.ai_coding_tools.common_stub import FakeCursorStore, _iso_ms, stubbed_common
from tests.fixtures.ai_coding_tools.cursor.build_state_db import (
    COMPOSER_DRAFT,
    COMPOSER_EMPTY_WINDOW,
    COMPOSER_JSONL,
    COMPOSER_NORMAL,
    COMPOSER_SUBAGENT_HEADER,
    COMPOSER_SUBAGENT_INFO,
    PROJECT_SLUG,
    append_bubble,
    build_state_db,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ai_coding_tools" / "cursor"

TOTAL_ROWS = 10  # normal 5 + empty-window 2 + JSONL 3 (its 2 DB bubbles are not emitted)


@pytest.fixture(scope="module")
def reader():
    with stubbed_common():
        yield importlib.import_module("topos.ingestion.sources.cursor_reader")


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    home = tmp_path / "cursor_home"
    shutil.copytree(FIXTURES / "projects", home / "projects")
    user_dir = tmp_path / "CursorUser"
    db = build_state_db(user_dir)
    jsonl = home / "projects" / PROJECT_SLUG / "agent-transcripts" / COMPOSER_JSONL / f"{COMPOSER_JSONL}.jsonl"
    assert jsonl.exists()
    return SimpleNamespace(home=home, user_dir=user_dir, db=db, jsonl=jsonl)


@pytest.fixture
def writer(env: SimpleNamespace):
    """A concurrent Cursor stand-in: writes stay in the WAL (no checkpoint)."""
    conn = sqlite3.connect(str(env.db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    try:
        yield conn
    finally:
        conn.close()


def _read(reader, env: SimpleNamespace, store: FakeCursorStore | None = None, **kw):
    return reader.read_cursor_batch(
        root_path=env.home, state_db_path=env.db, cursors=store or FakeCursorStore(), **kw
    )


def _thread(batch, composer_id: str):
    return [r for r in batch.rows if r["thread_id"] == composer_id]


def _row(batch, record_id: str):
    hits = [r for r in batch.rows if r["id"] == record_id]
    assert len(hits) == 1, record_id
    return hits[0]


def test_bubble_written_to_wal_after_first_open_is_seen_on_next_call(reader, env, writer):
    store = FakeCursorStore()
    first = _read(reader, env, store)
    assert len(first.rows) == TOTAL_ROWS
    main_mtime = env.db.stat().st_mtime_ns
    append_bubble(writer, COMPOSER_NORMAL, {
        "bubbleId": "bubble-n13", "type": 2, "text": "Done.", "createdAt": "2026-10-15T10:06:00.000Z",
    })
    wal = Path(str(env.db) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    assert env.db.stat().st_mtime_ns == main_mtime  # the write lives only in the WAL
    second = _read(reader, env, store)
    assert [r["id"] for r in second.rows] == ["cursor:bubble-n13"]
    assert second.rows[0]["_metadata"]["turn_index"] == 5
    assert second.rows[0]["_metadata"]["share_excluded"] is True  # taint from bubble-n04 persists across runs
    assert second.files_touched == 1


def test_composer_whose_bubble_count_grew_without_timestamp_change_is_picked_up(reader, env, writer):
    """Spikes A+B reconciled: timestamps are sparse (lastUpdatedAt on 89/431
    composers, createdAt missing on many bubbles), so the header count is a
    change signal in its own right. The empty-window composer carries no
    timestamp anywhere; a new bubble without createdAt must still arrive."""
    store = FakeCursorStore()
    assert len(_read(reader, env, store).rows) == TOTAL_ROWS
    append_bubble(writer, COMPOSER_EMPTY_WINDOW, {
        "bubbleId": "bubble-e03", "type": 1, "text": "And how do I abort one?",
    })
    second = _read(reader, env, store)
    assert [r["id"] for r in second.rows] == ["cursor:bubble-e03"]
    assert second.rows[0]["_metadata"]["timestamp_source"] == "file_mtime"
    assert second.rows[0]["_metadata"]["turn_index"] == 2
    third = _read(reader, env, store)
    assert third.rows == [] and third.files_touched == 0


def test_tool_former_user_bubbles_are_never_human(reader, env):
    batch = _read(reader, env)
    assert "cursor:bubble-n03" not in {r["id"] for r in batch.rows}
    assert batch.ledger.dropped["tool_bubble"] == 1
    assert {r["id"] for r in batch.rows if r["role"] == "user" and r["_metadata"]["origin"] == "composer"} == {
        "cursor:bubble-n01", "cursor:bubble-n06", "cursor:bubble-e01",
    }


def test_empty_window_workspace_gives_project_key_null(reader, env):
    batch = _read(reader, env)
    empty = _thread(batch, COMPOSER_EMPTY_WINDOW)
    assert [r["id"] for r in empty] == ["cursor:bubble-e01", "cursor:bubble-e02"]
    assert [r["project_key"] for r in empty] == [None, None]
    assert {r["project_key"] for r in _thread(batch, COMPOSER_NORMAL)} == {"beta-project"}


def test_timestamp_source_recorded_per_fallback(reader, env):
    batch = _read(reader, env)
    n01 = _row(batch, "cursor:bubble-n01")
    n02 = _row(batch, "cursor:bubble-n02")
    n05 = _row(batch, "cursor:bubble-n05")
    e01 = _row(batch, "cursor:bubble-e01")
    assert (n01["created_at"], n01["_metadata"]["timestamp_source"]) == ("2026-10-15T10:00:00.000Z", "record")
    assert (n02["created_at"], n02["_metadata"]["timestamp_source"]) == ("2026-10-15T10:00:30.000Z", "record")
    assert (n05["created_at"], n05["_metadata"]["timestamp_source"]) == ("2026-10-15T09:59:00.000Z", "session")
    expected_mtime = _iso_ms(datetime.fromtimestamp(env.db.stat().st_mtime, timezone.utc))
    assert (e01["created_at"], e01["_metadata"]["timestamp_source"]) == (expected_mtime, "file_mtime")


def test_agent_transcripts_jsonl_preferred_over_db_bubbles(reader, env):
    batch = _read(reader, env)
    rows = _thread(batch, COMPOSER_JSONL)
    assert [r["content"] for r in rows] == ["Scaffold the CLI", "Scaffolded the entry point.", "Listed the files."]
    assert {r["_metadata"]["origin"] for r in rows} == {"agent_transcript"}
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
    assert {r["_metadata"]["conversation_title"] for r in rows} == {"CLI scaffolding"}
    assert {r["project_key"] for r in rows} == {"beta-project"}
    assert not {"cursor:bubble-j01", "cursor:bubble-j02"} & {r["id"] for r in batch.rows}
    assert batch.composers_jsonl_preferred == 1
    assert batch.ledger.dropped["tool_use"] == 2  # bubble-n04 + the JSONL tool_use block


def test_deleted_jsonl_between_runs_does_not_rewind(reader, env, writer):
    store = FakeCursorStore()
    first = _read(reader, env, store)
    assert len(first.rows) == TOTAL_ROWS
    env.jsonl.unlink()
    append_bubble(writer, COMPOSER_JSONL, {
        "bubbleId": "bubble-j03", "type": 2, "text": "Added a --help flag.", "createdAt": "2026-10-15T11:05:00.000Z",
    })
    second = _read(reader, env, store)
    assert [r["id"] for r in second.rows] == ["cursor:bubble-j03"]


def test_deleted_jsonl_without_prior_db_alignment_does_not_rewind(reader, env, writer):
    store = FakeCursorStore()
    # Pretend the DB was already consumed as-is, so the first run reads the JSONL only.
    stat = env.db.stat()
    store.put(reader.SOURCE_ID, env.db, size=stat.st_size, mtime=stat.st_mtime, offset=0, record_count=0)
    first = _read(reader, env, store)
    assert [r["thread_id"] for r in first.rows] == [COMPOSER_JSONL] * 3
    env.jsonl.unlink()
    append_bubble(writer, COMPOSER_JSONL, {
        "bubbleId": "bubble-j03", "type": 2, "text": "Added a --help flag.", "createdAt": "2026-10-15T11:05:00.000Z",
    })
    second = _read(reader, env, store)
    assert _thread(second, COMPOSER_JSONL) == []
    assert second.composers_no_rewind == 1
    assert len(second.rows) == 7  # the other composers, read for the first time


def test_subagent_composers_skipped_by_header_flag_and_subagent_info(reader, env):
    batch = _read(reader, env)
    assert _thread(batch, COMPOSER_SUBAGENT_HEADER) == []
    assert _thread(batch, COMPOSER_SUBAGENT_INFO) == []
    assert batch.ledger.dropped["subagent"] == 3


def test_spike_a_bubble_markers(reader, env):
    batch = _read(reader, env)
    assert [r["id"] for r in _thread(batch, COMPOSER_NORMAL)] == [
        "cursor:bubble-n01", "cursor:bubble-n02", "cursor:bubble-n05", "cursor:bubble-n06", "cursor:bubble-n12",
    ]
    assert "cursor:bubble-n99" not in {r["id"] for r in batch.rows}  # not in fullConversationHeadersOnly
    assert batch.ledger.dropped["non_keyboard_origin:simulated"] == 1
    assert batch.ledger.dropped["non_keyboard_origin:nudge"] == 1
    assert batch.ledger.dropped["non_keyboard_origin:subagent_spawn"] == 1
    assert batch.ledger.dropped["empty"] == 1
    assert batch.ledger.dropped["thinking"] == 1
    assert _thread(batch, COMPOSER_DRAFT) == []


def test_tool_former_topos_call_taints_following_assistant_bubbles_only(reader, env):
    batch = _read(reader, env)
    assert [(r["id"], r["_metadata"]["share_excluded"]) for r in _thread(batch, COMPOSER_NORMAL)] == [
        ("cursor:bubble-n01", False),
        ("cursor:bubble-n02", False),
        ("cursor:bubble-n05", True),
        ("cursor:bubble-n06", False),
        ("cursor:bubble-n12", True),
    ]
    assert batch.ledger.tainted_turns == 2


def test_unchanged_db_and_jsonl_are_not_reread(reader, env):
    store = FakeCursorStore()
    first = _read(reader, env, store)
    assert len(first.rows) == TOTAL_ROWS
    assert first.files_touched == 2 and first.scanned_files == 2
    second = _read(reader, env, store)
    assert second.rows == []
    assert second.files_touched == 0 and second.scanned_files == 2


def test_state_db_opened_read_only_uri_on_the_live_file(reader, env, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple] = []
    real_connect = reader.sqlite3.connect

    def spy(database, *args, **kwargs):
        calls.append((database, kwargs))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reader.sqlite3, "connect", spy)
    conn = reader._open_state_db(env.db)
    try:
        assert len(calls) == 1
        database, kwargs = calls[0]
        assert kwargs.get("uri") is True
        assert database == env.db.resolve().as_uri() + "?mode=ro"
        assert "immutable" not in database
        # P0.14: autocommit (no BEGIN held across the sync) with a short busy timeout
        assert "isolation_level" in kwargs and kwargs["isolation_level"] is None
        assert kwargs.get("timeout") == reader.STATE_DB_BUSY_TIMEOUT_SECONDS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO ItemTable VALUES ('x', 'y')")
    finally:
        conn.close()


def test_exclusive_lock_falls_back_to_a_clonefile_copy_read_only(reader, env, writer, monkeypatch: pytest.MonkeyPatch):
    """P0.14 fallback: the IDE holds an exclusive lock -> clone db + -wal, read the
    clone ``mode=ro`` (never immutable), delete it; the WAL-only bubble still arrives."""
    monkeypatch.setattr(reader, "STATE_DB_BUSY_TIMEOUT_SECONDS", 0.05)
    store = FakeCursorStore()
    assert len(_read(reader, env, store).rows) == TOTAL_ROWS
    writer.execute("PRAGMA locking_mode=EXCLUSIVE")
    append_bubble(writer, COMPOSER_NORMAL, {
        "bubbleId": "bubble-n13", "type": 2, "text": "Done.", "createdAt": "2026-10-15T10:06:00.000Z",
    })
    # Not vacuous: the live file really is locked for a plain read-only reader now.
    probe = sqlite3.connect(env.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            probe.execute("PRAGMA journal_mode").fetchone()
    finally:
        probe.close()

    calls: list[str] = []
    real_connect = reader.sqlite3.connect

    def spy(database, *args, **kwargs):
        calls.append(database)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(reader.sqlite3, "connect", spy)
    second = _read(reader, env, store)
    assert [r["id"] for r in second.rows] == ["cursor:bubble-n13"]
    assert calls[0] == env.db.resolve().as_uri() + "?mode=ro"
    assert len(calls) == 2 and calls[1] != calls[0]
    assert calls[1].endswith("?mode=ro") and "immutable" not in calls[1]
    clone_dir = Path(unquote(urlparse(calls[1].split("?")[0]).path)).parent
    assert clone_dir.name.startswith(reader._CLONE_PREFIX)
    assert not clone_dir.exists()  # the clone is deleted once the batch closes it


def test_titles_from_composer_name_else_first_user_bubble(reader, env):
    batch = _read(reader, env)
    assert {r["_metadata"]["conversation_title"] for r in _thread(batch, COMPOSER_NORMAL)} == {
        "Deterministic flaky test"
    }
    assert {r["_metadata"]["conversation_title"] for r in _thread(batch, COMPOSER_EMPTY_WINDOW)} == {
        "Quick question about git rebase"
    }


def test_session_path_jsonl_reads_only_that_file(reader, env):
    batch = _read(reader, env, session_path=str(env.jsonl))
    assert [r["thread_id"] for r in batch.rows] == [COMPOSER_JSONL] * 3
    assert batch.scanned_files == 1
