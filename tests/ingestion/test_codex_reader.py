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
THREAD_H = "0a1b2c3d-8888-4888-8888-a8a8a8a80008"
THREAD_I = "0a1b2c3d-9999-4999-8999-a9a9a9a90009"
OPEN_TURN_ROOT = FIXTURE_ROOT.parent / "codex_open_turn"
EDGES_ROOT = FIXTURE_ROOT.parent / "codex_edges"
THREAD_J = "0a1b2c3d-aaaa-4aaa-8aaa-aaaaaaaa000a"
THREAD_K = "0a1b2c3d-bbbb-4bbb-8bbb-bbbbbbbb000b"
FORK_FILE = DAY / f"rollout-2026-10-15T10-05-00-{THREAD_B}.jsonl"

TOTAL_ROWS = 23  # A 4 + B 2 + C 0 + D 4 + E 3 + F 0 + G 3 + H 7


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
    assert batch.scanned_files == 8
    assert [r["id"] for r in _thread(batch, THREAD_D)] == [
        "codex:msg_d_u1", "codex:msg_d_a1", "codex:msg_d_a2", "codex:msg_d_u2",
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
        ("codex:msg_d_u2", False),
    ]
    # a2 and a3 are one reply after the call; a1 before it keeps its own, shareable row.
    assert _row(batch, "codex:msg_d_a2")["content"] == "The node is running and paired.\n\nNothing else to do."
    assert batch.ledger.tainted_turns == 2  # D: 1 (shell topos-node), G: 1 (mcp_attribution)
    assert batch.ledger.dropped["tool_use"] == 5  # D 1, G 1, H 3
    assert batch.ledger.dropped["tool_result"] == 5


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
    assert batch.ledger.dropped["thinking"] == 2  # E 1, H 1


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
    assert batch.ledger.dropped["harness_injected"] == 5  # G: auto-compact u2 + a2, harness-only u3; H: auto-compact u2 + a5
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
    # A batch ends only between replies, so a reply is never cut in two by the batch size.
    assert sizes == [5, 5, 5, 5, 3]
    assert ids == full
    assert d_turns == [0, 1, 2, 3]
    final = _read(reader, codex_root, store)
    assert final.rows == []
    assert final.files_touched == 0
    assert final.scanned_files == 8


def test_since_unix_filters_records(reader, codex_root):
    since = datetime(2026, 10, 15, 10, 20, tzinfo=timezone.utc).timestamp()
    batch = _read(reader, codex_root, since_unix=since)
    assert sorted(r["id"] for r in batch.rows) == sorted([
        "codex:msg_e_u1", "codex:msg_e_a1", "codex:msg_e_a2",
        "codex:msg_g_u1", "codex:msg_g_a1", "codex:msg_g_a4",
        "codex:msg_h_u1", "codex:msg_h_a1", "codex:msg_h_a4", "codex:msg_h_u3",
        "codex:msg_h_a6", "codex:msg_h_a7", "codex:msg_h_a8",
    ])
    assert batch.newest_created_at == "2026-10-15T10:35:23.000Z"


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


# ---------------------------------------------------------------------------
# One row per reply: consecutive assistant messages of one turn and one phase
# ---------------------------------------------------------------------------


def test_the_commentary_of_one_turn_is_one_row_and_the_final_answer_another(reader, codex_root):
    batch = _read(reader, codex_root)
    rows = _thread(batch, THREAD_H)
    assert [r["id"] for r in rows] == [
        "codex:msg_h_u1", "codex:msg_h_a1", "codex:msg_h_a4", "codex:msg_h_u3",
        "codex:msg_h_a6", "codex:msg_h_a7", "codex:msg_h_a8",
    ]
    commentary = _row(batch, "codex:msg_h_a1")
    assert commentary["content"] == (
        "Profiling the import first.\n\nMost of the time goes to the YAML parse.\n\nSwitched it to the C loader."
    )
    # Id and stamp are the first message's; the tool calls between are not in the text.
    assert commentary["created_at"] == "2026-10-15T10:35:05.000Z"
    assert "yaml 0.41s" not in commentary["content"]
    assert [r["_metadata"]["origin"] for r in rows] == [
        "vscode", "commentary", "final_answer", "vscode", "commentary", "commentary", "final_answer",
    ]
    assert [r["_metadata"]["turn_index"] for r in rows] == [0, 1, 2, 3, 4, 5, 6]
    assert batch.ledger.merged_blocks == 3  # H: a2 + a3 into a1; D: a3 into a2


def test_a_new_turn_id_splits_the_reply_without_a_user_message(reader, codex_root):
    batch = _read(reader, codex_root)
    assert _row(batch, "codex:msg_h_a6")["content"] == "Running them now."
    assert _row(batch, "codex:msg_h_a7")["content"] == "A heartbeat turn picked this up: all 40 pass."


def test_an_auto_compact_turn_in_the_real_passthrough_shape_is_harness(reader, codex_root):
    # Real rollouts carry turn_id in payload.internal_chat_message_metadata_passthrough, nowhere else.
    batch = _read(reader, codex_root)
    ids = {r["id"] for r in batch.rows}
    assert "codex:msg_h_u2" not in ids and "codex:msg_h_a5" not in ids
    assert not any("Summarize our progress" in r["content"] for r in batch.rows)


def _open_turn_root(tmp_path: Path, *, age_seconds: float) -> tuple[Path, Path]:
    import os
    import time

    root = tmp_path / "codex_open"
    shutil.copytree(OPEN_TURN_ROOT, root, ignore=shutil.ignore_patterns("tail.jsonl"))
    path = next((root / DAY).glob("rollout-*.jsonl"))
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return root, path


def test_a_turn_still_running_is_held_and_merged_on_the_next_run(reader, tmp_path):
    root, path = _open_turn_root(tmp_path, age_seconds=60)
    lines = path.read_bytes().splitlines(keepends=True)
    store = FakeCursorStore()
    first = _read(reader, root, store)
    assert [r["id"] for r in first.rows] == ["codex:msg_i_u1"]
    # The cursor waits at the reply's first message; the tool call after it is not counted yet.
    assert store.get(reader.SOURCE_ID, str(path)).offset == sum(len(line) for line in lines[:3])
    assert store.get(reader.SOURCE_ID, str(path)).record_count == 1
    assert first.ledger.dropped.get("tool_use") is None
    again = _read(reader, root, store)
    assert again.rows == [] and again.files_touched == 0
    with path.open("ab") as handle:
        handle.write((OPEN_TURN_ROOT / "tail.jsonl").read_bytes())
    second = _read(reader, root, store)
    assert [r["id"] for r in second.rows] == ["codex:msg_i_a1", "codex:msg_i_a3"]
    assert second.rows[0]["content"] == "Starting the slow suite.\n\nAll 12 slow tests passed."
    assert second.rows[0]["created_at"] == "2026-10-15T10:40:03.000Z"
    assert [r["_metadata"]["turn_index"] for r in second.rows] == [1, 2]
    assert second.ledger.dropped["tool_use"] == 1 and second.ledger.dropped["tool_result"] == 1


def test_a_running_turn_in_a_rollout_quiet_for_an_hour_is_emitted(reader, tmp_path):
    root, path = _open_turn_root(tmp_path, age_seconds=2 * 3600)
    store = FakeCursorStore()
    batch = _read(reader, root, store)
    assert [r["id"] for r in batch.rows] == ["codex:msg_i_u1", "codex:msg_i_a1"]
    assert store.get(reader.SOURCE_ID, str(path)).offset == path.stat().st_size


def _edges_root(tmp_path: Path, *, age_seconds: float) -> Path:
    import os
    import time

    root = tmp_path / "codex_edges"
    shutil.copytree(EDGES_ROOT, root)
    stamp = time.time() - age_seconds
    for path in (root / DAY).glob("rollout-*.jsonl"):
        os.utime(path, (stamp, stamp))
    return root


def test_turn_edge_events_taint_and_compaction_each_end_a_reply(reader, tmp_path):
    batch = _read(reader, _edges_root(tmp_path, age_seconds=2 * 3600))
    rows = _thread(batch, THREAD_J)
    assert [(r["id"], r["content"], r["_metadata"]["share_excluded"]) for r in rows] == [
        ("codex:msg_j_u1", "Tidy the logging", False),
        ("codex:msg_j_a1", "Starting with the formatter.", False),
        ("codex:msg_j_a2", "Now the handlers.", False),
        ("codex:msg_j_a3", "The node is up; the handlers are next.", True),
        ("codex:msg_j_a4", "The handlers are quieter now.", True),
        ("codex:msg_j_a5", "Logging is tidy.\n\nThe formatter keeps the old format.", True),
    ]


def test_a_batch_of_one_keeps_a_reply_whole_after_a_phase_change(reader, tmp_path):
    # The commentary row fills the batch and the final answer opens in the same step: it is still one row.
    root = _edges_root(tmp_path, age_seconds=2 * 3600)
    path = next((root / DAY).glob(f"*{THREAD_J}.jsonl"))
    store = FakeCursorStore()
    rows = []
    for _ in range(20):
        batch = _read(reader, root, store, batch_size=1, session_path=str(path))
        if not batch.rows:
            break
        rows.extend(batch.rows)
    assert [r["id"] for r in rows] == [f"codex:msg_j_{n}" for n in ("u1", "a1", "a2", "a3", "a4", "a5")]
    assert rows[-1]["content"] == "Logging is tidy.\n\nThe formatter keeps the old format."


def test_only_the_running_turn_is_held_when_the_turn_id_changes_without_an_event(reader, tmp_path):
    root = _edges_root(tmp_path, age_seconds=60)
    store = FakeCursorStore()
    batch = _read(reader, root, store)
    assert [r["id"] for r in _thread(batch, THREAD_K)] == ["codex:msg_k_u1", "codex:msg_k_a1"]
    path = next((root / DAY).glob(f"*{THREAD_K}.jsonl"))
    lines = path.read_bytes().splitlines(keepends=True)
    assert store.get(reader.SOURCE_ID, str(path)).offset == sum(len(line) for line in lines[:5])


def test_a_batch_of_one_never_cuts_a_reply_in_two(reader, codex_root):
    path = codex_root / DAY / f"rollout-2026-10-15T10-35-00-{THREAD_H}.jsonl"
    store = FakeCursorStore()
    rows = []
    for _ in range(20):
        batch = _read(reader, codex_root, store, batch_size=1, session_path=str(path))
        if not batch.rows:
            break
        rows.extend(batch.rows)
    assert [r["id"] for r in rows] == [
        "codex:msg_h_u1", "codex:msg_h_a1", "codex:msg_h_a4", "codex:msg_h_u3",
        "codex:msg_h_a6", "codex:msg_h_a7", "codex:msg_h_a8",
    ]
    assert rows[1]["content"].count("\n\n") == 2
