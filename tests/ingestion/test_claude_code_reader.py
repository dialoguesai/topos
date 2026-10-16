"""Claude Code transcript reader over the synthetic fixture trees (CONTRACTS §2.2–2.4, §2.7, §2.7b).

Every count below is a literal derived from ``tests/fixtures/ai_coding_tools/generate_fixtures.py``.
No fixture holds owner data; no test reads anything outside ``tests/fixtures`` and ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from topos.ingestion.checkpoints.file_cursors import FileCursorStore
from topos.ingestion.sources.claude_code_reader import SOURCE_ID, TAINT_MARKER_SUFFIX, read_claude_code_batch

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ai_coding_tools" / "claude_code"
SLUG = "-Users-fake-dev-widget"
ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$")


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


@pytest.fixture()
def cursors():
    conn = sqlite3.connect(":memory:")
    yield FileCursorStore(conn)
    conn.close()


def read(case: str, cursors, **kwargs):
    return read_claude_code_batch(root_path=FIXTURES / case, cursors=cursors, hostnames=("fakebox-7",), usernames=("fake",), **kwargs)


def roles(rows) -> list[str]:
    return [row["role"] for row in rows]


def by_uuid(rows, n: int) -> dict:
    matches = [row for row in rows if row["id"] == f"claude_code:{uid(n)}"]
    assert len(matches) == 1, f"expected one row for {uid(n)}, got {len(matches)}"
    return matches[0]


def assert_record_shape(rows) -> None:
    for row in rows:
        assert set(row) == {"id", "thread_id", "role", "content", "created_at", "_metadata"}
        assert row["id"].startswith("claude_code:")
        assert row["role"] in ("user", "assistant")
        assert row["content"].strip() == row["content"] and row["content"]
        assert ISO_MS.match(row["created_at"]), row["created_at"]
        meta = row["_metadata"]
        assert meta["tool"] == "claude_code"
        assert meta["session_id"] == row["thread_id"]
        assert isinstance(meta["conversation_title"], str) and meta["conversation_title"]
        assert meta["origin"] == ("keyboard" if row["role"] == "user" else "assistant")
        assert isinstance(meta["share_excluded"], bool)
        assert meta["timestamp_source"] == "record"
        blob = json.dumps(row)
        for forbidden in ("cwd", "/Users/", "/home/", "gitBranch", "-Users-"):
            assert forbidden not in blob, (forbidden, blob)


# ---------------------------------------------------------------------------
# Plain typed turns, grouping by message.id, ids, stamps, titles
# ---------------------------------------------------------------------------


def test_plain_session_emits_typed_turns_and_replies_with_exact_counts(cursors) -> None:
    batch = read("plain", cursors)
    assert_record_shape(batch.rows)
    assert roles(batch.rows) == ["user", "assistant", "user", "assistant"]
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (1, 2, 3, 4)]
    assert batch.rows[3]["content"] == "Done: three attempts.\n\nEach attempt waits with jitter."
    assert [row["_metadata"]["turn_index"] for row in batch.rows] == [0, 1, 2, 3]
    assert [row["created_at"] for row in batch.rows] == [
        "2026-10-01T09:00:00.000+00:00",
        "2026-10-01T09:00:00.250+00:00",
        "2026-10-01T09:00:00.500+00:00",
        "2026-10-01T09:00:00.750+00:00",
    ]
    assert {row["thread_id"] for row in batch.rows} == {uid(0x101)}
    assert {row["_metadata"]["conversation_title"] for row in batch.rows} == {"Add a retry to the uploader"}
    assert all(row["_metadata"]["share_excluded"] is False for row in batch.rows)
    assert batch.rows[1]["_metadata"]["model_slug"] == "claude-fable-5-1"
    assert "model_slug" not in batch.rows[0]["_metadata"]
    ledger = batch.ledger.as_dict()
    assert ledger["turns_emitted"] == 4
    assert ledger["dropped"] == {}
    assert ledger["message_nodes"] == 5
    assert ledger["merged_blocks"] == 1
    assert ledger["conversations_seen"] == 1 and ledger["conversations_kept"] == 1
    assert ledger["tainted_turns"] == 0 and ledger["scrubbed"] == {}
    assert batch.scanned_files == 1 and batch.files_touched == 1
    assert batch.newest_created_at == "2026-10-01T09:00:00.750+00:00"


def test_titles_come_from_custom_title_json_then_title_records_then_the_first_prompt(cursors) -> None:
    batch = read("titles", cursors)
    titles = {row["thread_id"]: row["_metadata"]["conversation_title"] for row in batch.rows}
    assert titles[uid(0x110)] == "My custom title"
    assert titles[uid(0x111)] == "Custom via record"
    long_title = titles[uid(0x112)]
    assert len(long_title) == 80
    assert long_title == (
        "Please walk me through the uploader's retry policy, the jitter it applies betwee"
    )
    assert all(row["_metadata"]["conversation_title"] for row in batch.rows)
    assert batch.ledger.as_dict()["meta_records"] == 3
    assert batch.ledger.as_dict()["turns_emitted"] == 6


def test_a_session_with_nothing_to_title_from_gets_the_fallback_on_every_record(cursors) -> None:
    batch = read("no_title", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(0x1A2)}"]
    assert batch.rows[0]["_metadata"]["conversation_title"] == "Claude Code session 00000116"
    assert batch.ledger.as_dict()["dropped"] == {"harness_injected": 1}


# ---------------------------------------------------------------------------
# Classification by content-block type and origin
# ---------------------------------------------------------------------------


def test_tool_result_inside_role_user_is_never_human(cursors) -> None:
    batch = read("tool_result_in_user", cursors)
    assert_record_shape(batch.rows)
    assert roles(batch.rows) == ["user", "assistant", "assistant"]
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x11, 0x12, 0x15)]
    assert not any("timeout = 5" in row["content"] for row in batch.rows)
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {"tool_result": 1, "tool_use": 1}
    assert ledger["turns_emitted"] == 3 and ledger["message_nodes"] == 5


def test_thinking_and_tool_use_blocks_are_dropped_and_counted(cursors) -> None:
    batch = read("thinking_and_tool_use", cursors)
    assert roles(batch.rows) == ["user", "assistant"]
    assert batch.rows[1]["content"] == "It's in uploader.py at line 42."
    assert not any("Need to grep" in row["content"] for row in batch.rows)
    assert batch.ledger.as_dict()["dropped"] == {"thinking": 1, "tool_result": 1, "tool_use": 1}


def test_harness_injected_and_non_keyboard_origins_are_never_human(cursors) -> None:
    batch = read("harness_injected", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x3B, 0x3D, 0x41, 0x42, 0x43)]
    assert roles(batch.rows) == ["user", "user", "user", "user", "assistant"]
    assert batch.rows[0]["content"] == "Please rename the module to uploads"
    assert batch.rows[1]["content"] == "Now run the tests"
    assert batch.rows[2]["content"] == "What's in this screenshot?"
    assert batch.rows[3]["content"] == "Typed in the desktop app"
    for row in batch.rows:
        assert "<" not in row["content"]
        assert "Lane L2" not in row["content"] and "Peer" not in row["content"]
        assert "interrupted" not in row["content"]
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {
        "harness_injected": 13,
        "non_keyboard_origin:isMeta": 2,
        "non_keyboard_origin:origin.kind=task-notification": 1,
        "non_keyboard_origin:promptSource=system": 1,
        "non_keyboard_origin:turnOrigin=peer": 1,
    }
    assert ledger["origins"] == {
        "artifact-view-context": 1,
        "bash-input": 1,
        "ci-monitor-event": 1,
        "command-message": 1,
        "command-name": 1,
        "create-pr-command": 1,
        "cross-session-message": 1,
        "ide_": 1,
        "interrupt_marker": 1,
        "isMeta": 2,
        "local-command-caveat": 1,
        "local-command-stdout": 1,
        "origin.kind=task-notification": 1,
        "promptSource=system": 1,
        "system-reminder": 1,
        "task-notification": 1,
        "turnOrigin=peer": 1,
    }
    assert ledger["harness_spans_stripped"] == 2
    assert ledger["turns_emitted"] == 5
    assert ledger["message_nodes"] == 23
    assert ledger["dropped_nodes_total"] == 18


def test_empty_content_is_dropped_with_reason_empty(cursors) -> None:
    batch = read("empty_turn", cursors)
    assert roles(batch.rows) == ["user", "assistant"]
    assert [row["content"] for row in batch.rows] == ["Real question", "Real answer."]
    assert batch.ledger.as_dict()["dropped"] == {"empty": 3}
    assert [row["_metadata"]["turn_index"] for row in batch.rows] == [0, 1]


# ---------------------------------------------------------------------------
# Compaction, forks, side files, skipped folders, windows
# ---------------------------------------------------------------------------


def test_compaction_chain_stays_one_conversation_with_monotonic_turn_index(cursors) -> None:
    batch = read("compaction", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x51, 0x52, 0x55, 0x56)]
    assert {row["thread_id"] for row in batch.rows} == {uid(0x105)}
    assert [row["_metadata"]["turn_index"] for row in batch.rows] == [0, 1, 2, 3]
    stamps = [row["created_at"] for row in batch.rows]
    assert stamps == sorted(stamps)
    assert {row["_metadata"]["conversation_title"] for row in batch.rows} == {"Uploader rewrite"}
    assert not any("being continued" in row["content"] for row in batch.rows)
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {"non_keyboard_origin:isCompactSummary": 1, "system": 1}
    assert ledger["meta_records"] == 1
    assert ledger["message_nodes"] == 7
    assert ledger["conversations_seen"] == 1


def test_fork_copy_yields_one_row_per_uuid(cursors, tmp_path) -> None:
    root = tmp_path / "fork_copy"
    shutil.copytree(FIXTURES / "fork_copy", root)
    orig = root / SLUG / f"{uid(0x106)}.jsonl"
    fork = root / SLUG / f"{uid(0x107)}.jsonl"
    os.utime(orig, (1_760_000_000, 1_760_000_000))
    os.utime(fork, (1_760_000_100, 1_760_000_100))
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    ids = [row["id"] for row in batch.rows]
    assert sorted(ids) == sorted(f"claude_code:{uid(n)}" for n in (0x61, 0x62, 0x63, 0x64))
    assert len(ids) == len(set(ids)) == 4
    assert by_uuid(batch.rows, 0x61)["thread_id"] == uid(0x106)
    assert by_uuid(batch.rows, 0x63)["thread_id"] == uid(0x107)
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {"fork_duplicate": 2}
    assert ledger["message_nodes"] == 6 and ledger["turns_emitted"] == 4
    assert batch.scanned_files == 2 and batch.files_touched == 2


def test_orphaned_and_superseded_side_files_are_skipped(cursors) -> None:
    batch = read("side_files", cursors)
    assert [row["content"] for row in batch.rows] == ["Hello", "Hi."]
    assert not any("Orphan" in row["content"] or "Superseded" in row["content"] for row in batch.rows)
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {"side_file": 2}
    assert ledger["dropped_files"] == {"side_file": 2}
    assert batch.scanned_files == 1 and batch.files_touched == 1


def test_skipped_folder_writes_nothing(cursors) -> None:
    batch = read("two_folders", cursors, skip_folders=("-Users-fake-dev-secretproj",))
    assert [row["content"] for row in batch.rows] == ["Widget work", "Sure."]
    assert {row["thread_id"] for row in batch.rows} == {uid(0x10D)}
    assert not any("Secret" in row["content"] for row in batch.rows)
    assert batch.ledger.as_dict()["dropped"] == {"skipped_folder": 1}
    assert batch.scanned_files == 1 and batch.files_touched == 1
    secret_file = FIXTURES / "two_folders" / "-Users-fake-dev-secretproj" / f"{uid(0x10E)}.jsonl"
    assert cursors.get(SOURCE_ID, str(secret_file)) is None


def test_skip_folders_also_match_the_scrubbed_slug_form(cursors) -> None:
    batch = read("two_folders", cursors, skip_folders=("-~-dev-secretproj",))
    assert {row["thread_id"] for row in batch.rows} == {uid(0x10D)}
    assert batch.ledger.as_dict()["dropped"] == {"skipped_folder": 1}


def test_since_unix_uses_record_timestamps_not_file_mtime(cursors, tmp_path) -> None:
    root = tmp_path / "since_window"
    shutil.copytree(FIXTURES / "since_window", root)
    old = root / SLUG / f"{uid(0x114)}.jsonl"
    new = root / SLUG / f"{uid(0x115)}.jsonl"
    # mtime says the old session is the newest file; its records say August.
    os.utime(old, (1_760_500_000, 1_760_500_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    since = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    batch = read_claude_code_batch(root_path=root, cursors=cursors, since_unix=since, hostnames=(), usernames=())
    assert [row["content"] for row in batch.rows] == ["New question", "New answer."]
    assert batch.ledger.as_dict()["dropped"] == {"outside_window": 1}
    assert batch.scanned_files == 2 and batch.files_touched == 1
    assert cursors.get(SOURCE_ID, str(old)) is None
    assert cursors.get(SOURCE_ID, str(new)) is not None


# ---------------------------------------------------------------------------
# Taint (§2.7)
# ---------------------------------------------------------------------------


def test_topos_call_taints_exactly_the_assistant_turns_after_it(cursors) -> None:
    batch = read("topos_call_then_three", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [
        f"claude_code:{uid(n)}" for n in (0xA1, 0xA2, 0xA5, 0xA6, 0xA7, 0xA8)
    ]
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0xA1)}": False,
        f"claude_code:{uid(0xA2)}": False,
        f"claude_code:{uid(0xA5)}": True,
        f"claude_code:{uid(0xA6)}": False,
        f"claude_code:{uid(0xA7)}": True,
        f"claude_code:{uid(0xA8)}": True,
    }
    assistant_after = [r for r in batch.rows if r["role"] == "assistant" and r["_metadata"]["share_excluded"]]
    assert len(assistant_after) == 3
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 3
    assert ledger["dropped"] == {"tool_result": 1, "tool_use": 1}


def test_a_taint_instant_known_up_front_still_spares_the_assistant_turns_before_it(cursors, tmp_path) -> None:
    # The in-file case above learns the instant mid-file; here it is already on
    # the marker (as after a sub-agent scan or an earlier run), so every reply
    # is judged by its stamp against it.
    root = tmp_path / "topos_call_then_three"
    shutil.copytree(FIXTURES / "topos_call_then_three", root)
    path = root / SLUG / f"{uid(0x10A)}.jsonl"
    call_epoch = datetime(2026, 10, 1, 9, 0, 0, 500_000, tzinfo=timezone.utc).timestamp()
    cursors.put(SOURCE_ID, str(path) + TAINT_MARKER_SUFFIX, size=0, mtime=call_epoch, offset=0, record_count=0)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0xA1)}": False,
        f"claude_code:{uid(0xA2)}": False,
        f"claude_code:{uid(0xA5)}": True,
        f"claude_code:{uid(0xA6)}": False,
        f"claude_code:{uid(0xA7)}": True,
        f"claude_code:{uid(0xA8)}": True,
    }
    assert batch.ledger.as_dict()["tainted_turns"] == 3


def test_bash_touching_topos_home_taints_the_session(cursors) -> None:
    batch = read("bash_touches_topos", cursors)
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0xB1)}": False,
        f"claude_code:{uid(0xB2)}": False,
        f"claude_code:{uid(0xB5)}": True,
        f"claude_code:{uid(0xB6)}": True,
    }
    assert batch.ledger.as_dict()["tainted_turns"] == 2


def test_subagent_only_taint_marks_the_parent_session(cursors) -> None:
    batch = read("subagent_taint", cursors)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x81, 0x82, 0x85, 0x86)]
    assert not any(row["id"] == f"claude_code:{uid(0x94)}" for row in batch.rows)
    assert not any("Found 2 mails" in row["content"] for row in batch.rows)
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0x81)}": False,
        f"claude_code:{uid(0x82)}": False,
        f"claude_code:{uid(0x85)}": True,
        f"claude_code:{uid(0x86)}": True,
    }
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 2
    assert ledger["subagent_files_scanned"] == 1
    assert ledger["dropped"] == {"tool_result": 1, "tool_use": 1}


def test_taint_persists_across_runs_so_later_turns_stay_excluded(cursors, tmp_path) -> None:
    root = tmp_path / "subagent_taint"
    shutil.copytree(FIXTURES / "subagent_taint", root)
    first = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert first.ledger.as_dict()["tainted_turns"] == 2
    main = root / SLUG / f"{uid(0x109)}.jsonl"
    late = {
        "parentUuid": uid(0x86), "isSidechain": False, "userType": "external", "cwd": "/Users/fake/dev/widget",
        "sessionId": uid(0x109), "version": "2.1.290", "type": "assistant", "uuid": uid(0x87),
        "timestamp": "2026-10-02T09:00:00.000Z",
        "message": {"id": "msg_m84", "role": "assistant", "model": "claude-fable-5-1",
                    "content": [{"type": "text", "text": "Later reply in the same session."}]},
    }
    with main.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(late) + "\n")
    second = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in second.rows] == [f"claude_code:{uid(0x87)}"]
    assert second.rows[0]["_metadata"]["share_excluded"] is True
    assert second.rows[0]["_metadata"]["turn_index"] == 4
    assert second.ledger.as_dict()["tainted_turns"] == 1


# ---------------------------------------------------------------------------
# Scrub (§2.4) and the key report
# ---------------------------------------------------------------------------


NODE_SECRETS = ("3f9c2b7e1a4d5c6b8e7f0a1b2c3d4e5f", "ok_" + "a1" * 24, "tpa_claude-code." + "b2" * 20)
INJECTED = NODE_SECRETS + (
    "tpn_" + "c3" * 20,
    "tpk_d4e5f6a7b8c9d0e1f2a3b4c5",
    "ok_" + "e5" * 10,
    "sk-ant-api03-" + "A" * 40,
    "sk-proj" + "B" * 20,
    "ghp_" + "C" * 36,
    "AKIADEFGHIJKLMNOPQRS",  # gitleaks:allow
    "xoxb-12345678ab-abcdefghijklmnop",
    "AIza" + "E" * 35,
    "F" * 30,
    "MIIEfakefakefake",
    "hunter2hunter2",
    "/Users/fake",
    "/home/fake",
    "-Users-fake",
    "fakebox.local",
    "fakebox-7",
)


def test_every_injected_secret_and_home_form_is_absent_from_every_row_and_counted(cursors) -> None:
    batch = read("secrets", cursors, secrets=NODE_SECRETS)
    assert roles(batch.rows) == ["user", "assistant", "user"]
    blob = json.dumps(batch.rows)
    for secret in INJECTED:
        assert secret not in blob, secret
    assert "fake" not in blob
    assert "[TOPOS_SECRET]" in batch.rows[0]["content"]
    assert "~/dev/widget/app.py" in batch.rows[1]["content"]
    assert "-~-dev-widget" in batch.rows[1]["content"]
    assert "[HOST]" in batch.rows[1]["content"]
    assert "DB_PASSWORD=[SECRET]" in batch.rows[2]["content"]
    ledger = batch.ledger.as_dict()
    assert ledger["scrubbed"] == {
        "anthropic_key": 1,
        "aws_access_key": 1,
        "bearer_token": 1,
        "env_assignment": 1,
        "github_token": 1,
        "google_api_key": 1,
        "home_path": 2,
        "home_slug": 2,
        "hostname_local": 1,
        "hostname_machine": 1,
        "openai_key": 1,
        "private_key_block": 1,
        "slack_token": 1,
        "topos_client_token": 1,
        "topos_node_key": 1,
        "topos_owner_key": 1,
        "topos_secret": 3,
    }
    assert ledger["secret_sightings"] == {uid(0x10C): 3}
    assert ledger["sessions_with_secrets"] == 1


def test_a_first_prompt_title_is_scrubbed_before_it_is_truncated(cursors) -> None:
    batch = read("secrets", cursors, secrets=NODE_SECRETS)
    title = batch.rows[0]["_metadata"]["conversation_title"]
    assert len(title) <= 80
    assert "3f9c2b7e" not in title and "ok_a1" not in title
    assert title.startswith("My env has TOPOS_KEY=[TOPOS_SECRET]")


# ---------------------------------------------------------------------------
# Cursors: no rewind, deleted files, partial lines, batch size, session_path
# ---------------------------------------------------------------------------


def test_a_second_run_reads_nothing_and_a_deleted_file_is_not_a_rewind(cursors, tmp_path) -> None:
    root = tmp_path / "plain"
    shutil.copytree(FIXTURES / "plain", root)
    path = root / SLUG / f"{uid(0x101)}.jsonl"
    first = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert len(first.rows) == 4
    before = cursors.get(SOURCE_ID, str(path))
    assert before is not None and before.offset == path.stat().st_size and before.record_count == 4
    again = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert again.rows == [] and again.files_touched == 0 and again.scanned_files == 1
    path.unlink()
    gone = read_claude_code_batch(root_path=root, cursors=cursors, session_path=str(path), hostnames=(), usernames=())
    assert gone.rows == []
    assert gone.ledger.as_dict()["dropped"] == {"file_gone": 1}
    assert cursors.get(SOURCE_ID, str(path)) == before


def test_a_partial_last_line_is_left_for_the_next_run(cursors, tmp_path) -> None:
    root = tmp_path / "plain"
    shutil.copytree(FIXTURES / "plain", root)
    path = root / SLUG / f"{uid(0x101)}.jsonl"
    whole = path.stat().st_size
    late = {
        "parentUuid": uid(5), "isSidechain": False, "userType": "external", "cwd": "/Users/fake/dev/widget",
        "sessionId": uid(0x101), "version": "2.1.290", "type": "user", "uuid": uid(6),
        "timestamp": "2026-10-01T09:00:02.000Z", "message": {"role": "user", "content": "Late typed line"},
    }
    line = json.dumps(late)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[:40])
    first = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert len(first.rows) == 4
    assert cursors.get(SOURCE_ID, str(path)).offset == whole
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[40:] + "\n")
    second = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["content"] for row in second.rows] == ["Late typed line"]
    assert second.rows[0]["_metadata"]["turn_index"] == 4
    assert cursors.get(SOURCE_ID, str(path)).offset == path.stat().st_size


def test_batch_size_is_a_soft_cap_checked_between_files(cursors) -> None:
    first = read("two_folders", cursors, batch_size=1)
    assert len(first.rows) == 2 and first.files_touched == 1
    second = read("two_folders", cursors, batch_size=1)
    assert len(second.rows) == 2 and second.files_touched == 1
    assert {first.rows[0]["thread_id"], second.rows[0]["thread_id"]} == {uid(0x10D), uid(0x10E)}
    third = read("two_folders", cursors, batch_size=1)
    assert third.rows == [] and third.files_touched == 0


def test_session_path_reads_one_file_and_still_scans_its_subagents(cursors) -> None:
    path = FIXTURES / "subagent_taint" / SLUG / f"{uid(0x109)}.jsonl"
    batch = read_claude_code_batch(
        root_path=FIXTURES / "plain", cursors=cursors, session_path=str(path), hostnames=(), usernames=(),
    )
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x81, 0x82, 0x85, 0x86)]
    assert batch.ledger.as_dict()["tainted_turns"] == 2
    assert batch.scanned_files == 1 and batch.files_touched == 1


def test_session_path_honours_skip_folders_and_side_files(cursors) -> None:
    skipped = FIXTURES / "two_folders" / "-Users-fake-dev-secretproj" / f"{uid(0x10E)}.jsonl"
    batch = read_claude_code_batch(
        root_path=FIXTURES / "two_folders", cursors=cursors, session_path=str(skipped),
        skip_folders=("-Users-fake-dev-secretproj",), hostnames=(), usernames=(),
    )
    assert batch.rows == [] and batch.ledger.as_dict()["dropped"] == {"skipped_folder": 1}
    side = FIXTURES / "side_files" / SLUG / f"{uid(0x108)}.jsonl.orphaned-20261001T090000Z"
    batch = read_claude_code_batch(
        root_path=FIXTURES / "side_files", cursors=cursors, session_path=str(side), hostnames=(), usernames=(),
    )
    assert batch.rows == [] and batch.ledger.as_dict()["dropped"] == {"side_file": 1}


def test_the_default_root_is_the_claude_projects_folder_but_is_never_opened_here() -> None:
    from topos.ingestion.sources import claude_code_reader

    assert claude_code_reader.DEFAULT_ROOT == Path.home() / ".claude" / "projects"
    assert SOURCE_ID == "claude_code_sessions"
