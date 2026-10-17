"""Guards for the Codex rollout reader (lane L6, CONTRACTS.md §2.2–2.4, §2.7, §2.7b).

Extraction only: ``common`` is the stub from ``tests/fixtures/ai_coding_tools``
(scrubbing is identity there), the cursor store is in-memory, and every root is
a copy of the synthetic fixture tree under ``tmp_path``. Nothing here opens the
real ``~/.codex``.
"""

from __future__ import annotations

import importlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fixtures.ai_coding_tools.codex.build_state_db import (
    THREAD_A,
    THREAD_B,
    TITLE_A,
    TITLE_B,
    build_state_db,
)
from tests.fixtures.ai_coding_tools.common_stub import FakeCursorStore, stubbed_common

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai_coding_tools" / "codex"
DAY = Path("sessions") / "2026" / "10" / "15"

THREAD_C = "0a1b2c3d-3333-4ccc-8ccc-cccccccc0003"
THREAD_D = "0a1b2c3d-4444-4ddd-8ddd-dddddddd0004"
THREAD_E = "0a1b2c3d-5555-4eee-8eee-eeeeeeee0005"
THREAD_F = "0a1b2c3d-6666-4fff-8fff-ffffffff0006"
THREAD_G = "0a1b2c3d-7777-4777-8777-a7a7a7a70007"
FORK_FILE = DAY / f"rollout-2026-10-15T10-05-00-{THREAD_B}.jsonl"

TOTAL_ROWS = 17  # A 4 + B 2 + C 0 + D 5 + E 3 + F 0 + G 3


@pytest.fixture(scope="module")
def reader():
    with stubbed_common():
        yield importlib.import_module("topos.ingestion.sources.codex_reader")


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    root = tmp_path / "codex_home"
    shutil.copytree(FIXTURE_ROOT, root, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    build_state_db(root)
    return root


def _read(reader, root: Path, store: FakeCursorStore | None = None, **kw):
    return reader.read_codex_batch(root_path=root, cursors=store or FakeCursorStore(), **kw)


def _thread(batch, thread_id: str):
    return [r for r in batch.rows if r["thread_id"] == thread_id]


def _row(batch, record_id: str):
    hits = [r for r in batch.rows if r["id"] == record_id]
    assert len(hits) == 1, record_id
    return hits[0]


def test_multiple_session_meta_lines_are_one_conversation(reader, codex_root):
    batch = _read(reader, codex_root, session_path=str(codex_root / FORK_FILE))
    assert batch.scanned_files == 1
    assert {r["thread_id"] for r in batch.rows} == {THREAD_B}
    # inherited_user_message marks the replayed user prompt even with no parent in the run
    assert [r["id"] for r in batch.rows] == [
        "codex:msg_a_a1", "codex:msg_a_u2", "codex:msg_b_u3", "codex:msg_b_a3",
    ]
    assert [r["_metadata"]["turn_index"] for r in batch.rows] == [0, 1, 2, 3]
    assert batch.ledger.dropped.get("fork_duplicate") == 1


def test_preview_drains_multiple_batches_with_real_reader_cursors(reader, codex_root, monkeypatch):
    from topos.ingestion import ai_tool_sync

    def read(**kwargs):
        return reader.read_codex_batch(**{**kwargs, "root_path": codex_root})

    monkeypatch.setattr(ai_tool_sync, "_reader_for", lambda tool: read)
    monkeypatch.setattr(ai_tool_sync, "node_secret_values", lambda conn: ())
    monkeypatch.setattr(ai_tool_sync, "DEFAULT_BATCH_SIZE", 2)
    result = ai_tool_sync.preview_ai_tool_source(None, "codex_sessions")
    assert result["sessions"] > 0
    assert result["turns"] > 2
    assert result["bytes_text"] > 0


def test_fork_replay_deduped_across_files_first_writer_wins(reader, codex_root):
    batch = _read(reader, codex_root)
    assert len(batch.rows) == TOTAL_ROWS
    assert len({r["id"] for r in batch.rows}) == TOTAL_ROWS
    assert [r["id"] for r in _thread(batch, THREAD_B)] == ["codex:msg_b_u3", "codex:msg_b_a3"]
    assert [r["thread_id"] for r in batch.rows if r["id"] == "codex:msg_a_u1"] == [THREAD_A]
    assert batch.ledger.dropped["fork_duplicate"] == 3


def test_subagent_threads_skipped(reader, codex_root):
    batch = _read(reader, codex_root)
    assert _thread(batch, THREAD_C) == []
    assert batch.ledger.dropped["subagent"] == 2


def test_rollout_id_filename_variant_accepted(reader, codex_root):
    batch = _read(reader, codex_root)
    assert batch.scanned_files == 7
    assert [r["id"] for r in _thread(batch, THREAD_D)] == [
        "codex:msg_d_u1", "codex:msg_d_a1", "codex:msg_d_a2", "codex:msg_d_a3", "codex:msg_d_u2",
    ]


def test_environment_context_block_stripped_from_user_text(reader, codex_root):
    batch = _read(reader, codex_root)
    row = _row(batch, "codex:msg_d_u1")
    assert row["role"] == "user"
    assert row["content"] == "Check the node status with the topos CLI"


def test_topos_function_call_taints_following_assistant_records_only(reader, codex_root):
    batch = _read(reader, codex_root)
    assert [(r["id"], r["_metadata"]["share_excluded"]) for r in _thread(batch, THREAD_D)] == [
        ("codex:msg_d_u1", False),
        ("codex:msg_d_a1", False),
        ("codex:msg_d_a2", True),
        ("codex:msg_d_a3", True),
        ("codex:msg_d_u2", False),
    ]
    assert batch.ledger.tainted_turns == 3  # D: 2 (shell topos-node), G: 1 (mcp_attribution)
    assert batch.ledger.dropped["tool_use"] == 2
    assert batch.ledger.dropped["tool_result"] == 2


def test_imported_sessions_flagged_cross_tool_import(reader, codex_root):
    batch = _read(reader, codex_root)
    imported = _thread(batch, THREAD_E)
    assert len(imported) == 3
    assert all(r["_metadata"]["cross_tool_import"] is True for r in imported)
    assert all(not r["_metadata"].get("cross_tool_import") for r in batch.rows if r["thread_id"] != THREAD_E)
    assert batch.imported_sessions == 1


def test_commentary_and_final_answer_kept_as_separate_records(reader, codex_root):
    batch = _read(reader, codex_root)
    rows = _thread(batch, THREAD_E)
    assert [r["id"] for r in rows] == ["codex:msg_e_u1", "codex:msg_e_a1", "codex:msg_e_a2"]
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
    assert [r["_metadata"]["origin"] for r in rows] == ["vscode", "commentary", "final_answer"]
    assert [r["_metadata"].get("model_slug") for r in rows] == [None, "gpt-5-codex", "gpt-5-codex"]
    assert batch.ledger.dropped["thinking"] == 1


def test_non_keyboard_source_thread_dropped(reader, codex_root):
    batch = _read(reader, codex_root)
    assert _thread(batch, THREAD_F) == []
    assert batch.ledger.dropped["non_keyboard_origin:exec"] == 2


def test_spike_a_markers_developer_role_harness_blocks_auto_compact_mcp_attribution(reader, codex_root):
    batch = _read(reader, codex_root)
    rows = _thread(batch, THREAD_G)
    assert [r["id"] for r in rows] == ["codex:msg_g_u1", "codex:msg_g_a1", "codex:msg_g_a4"]
    assert rows[0]["content"] == "Rename the helper"
    assert batch.ledger.dropped["system"] == 1  # developer role
    assert batch.ledger.dropped["harness_injected"] == 3  # auto-compact u2 + a2, harness-only u3
    assert [r["_metadata"]["share_excluded"] for r in rows] == [False, False, True]


def test_titles_from_state_db_else_first_user_prompt(reader, codex_root):
    batch = _read(reader, codex_root)
    assert {r["_metadata"]["conversation_title"] for r in _thread(batch, THREAD_A)} == {TITLE_A}
    assert {r["_metadata"]["conversation_title"] for r in _thread(batch, THREAD_B)} == {TITLE_B}
    assert {r["_metadata"]["conversation_title"] for r in _thread(batch, THREAD_D)} == {
        "Check the node status with the topos CLI"
    }


def test_byte_offset_cursor_resumes_without_duplicates(reader, codex_root):
    full = [r["id"] for r in _read(reader, codex_root).rows]
    store = FakeCursorStore()
    ids: list[str] = []
    d_turns: list[int] = []
    sizes: list[int] = []
    for _ in range(10):
        batch = _read(reader, codex_root, store, batch_size=5)
        if not batch.rows:
            break
        sizes.append(len(batch.rows))
        ids.extend(r["id"] for r in batch.rows)
        d_turns.extend(r["_metadata"]["turn_index"] for r in batch.rows if r["thread_id"] == THREAD_D)
    assert sizes == [5, 5, 5, 2]
    assert ids == full
    assert d_turns == [0, 1, 2, 3, 4]
    final = _read(reader, codex_root, store)
    assert final.rows == []
    assert final.files_touched == 0
    assert final.scanned_files == 7


def test_since_unix_filters_records(reader, codex_root):
    since = datetime(2026, 10, 15, 10, 20, tzinfo=timezone.utc).timestamp()
    batch = _read(reader, codex_root, since_unix=since)
    assert sorted(r["id"] for r in batch.rows) == sorted([
        "codex:msg_e_u1", "codex:msg_e_a1", "codex:msg_e_a2",
        "codex:msg_g_u1", "codex:msg_g_a1", "codex:msg_g_a4",
    ])
    assert batch.newest_created_at == "2026-10-15T10:32:05.000Z"


def test_project_key_outside_record_and_skip_folders(reader, codex_root):
    batch = _read(reader, codex_root)
    assert {r["project_key"] for r in _thread(batch, THREAD_A)} == {"alpha-project"}
    assert all("cwd" not in r["_metadata"] and "/" not in r["project_key"] for r in batch.rows)
    skipped = _read(reader, codex_root, skip_folders=("alpha-project",))
    assert _thread(skipped, THREAD_A) == [] and _thread(skipped, THREAD_B) == []
    assert len(skipped.rows) == TOTAL_ROWS - 6
    assert skipped.ledger.dropped["skipped_folder"] == 9  # A 4 + B 5 (replays included)


def test_default_root_is_codex_home_env(reader, codex_root, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(codex_root))
    batch = reader.read_codex_batch(cursors=FakeCursorStore())
    assert len(batch.rows) == TOTAL_ROWS
    monkeypatch.delenv("CODEX_HOME")
    assert reader._default_root() == Path.home() / ".codex"


def test_a_hooks_session_path_outside_the_codex_root_reads_nothing(reader, codex_root, tmp_path):
    """Security re-check N4: the sync door's session_path is confined to the tool's own folder."""
    outside = tmp_path / "not-the-codex-root" / "rollout-2026-10-01T09-00-00-6f1c2a3b-9d4e-4f50-8a61-7b2c3d4e5f60.jsonl"
    outside.parent.mkdir()
    outside.write_text('{"timestamp": "2026-10-01T09:00:00.000Z", "type": "session_meta", "payload": {"id": "thread-zz9"}}\n')
    batch = _read(reader, codex_root, session_path=str(outside))
    assert batch.rows == [] and batch.scanned_files == 0
    assert batch.ledger.dropped.get("outside_root") == 1
