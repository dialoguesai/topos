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
ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


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
        # The §2.2 record plus the one top-level facet L5 lifts off it (CONTRACTS §2.4): never anything else.
        assert set(row) - {"project_key"} == {"id", "thread_id", "role", "content", "created_at", "_metadata"}
        assert row.get("project_key") is None or (isinstance(row["project_key"], str) and row["project_key"])
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
        assert "project_key" not in meta and "cwd" not in meta
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
        "2026-10-01T09:00:00.000Z",
        "2026-10-01T09:00:00.250Z",
        "2026-10-01T09:00:00.500Z",
        "2026-10-01T09:00:00.750Z",
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
    assert batch.newest_created_at == "2026-10-01T09:00:00.750Z"


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
    assert roles(batch.rows) == ["user", "assistant"]
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x11, 0x12)]
    # The reply reads across the tool call it made; the tool's output is not in it.
    assert batch.rows[1]["content"] == "Reading it now.\n\nThe config sets a 5s timeout and 3 retries."
    assert not any("timeout = 5" in row["content"] for row in batch.rows)
    ledger = batch.ledger.as_dict()
    assert ledger["dropped"] == {"tool_result": 1, "tool_use": 1}
    assert ledger["turns_emitted"] == 2 and ledger["message_nodes"] == 5
    assert ledger["merged_blocks"] == 1


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


# ---------------------------------------------------------------------------
# The project facet (§2.6): the folder's basename rides top-level, and is what --skip takes
# ---------------------------------------------------------------------------


def test_rows_carry_the_project_folder_basename_as_a_top_level_facet(cursors) -> None:
    batch = read("two_folders", cursors)
    assert_record_shape(batch.rows)
    keys = {row["thread_id"]: row["project_key"] for row in batch.rows}
    assert keys == {uid(0x10D): "widget", uid(0x10E): "secretproj"}
    assert all("project_key" not in row["_metadata"] and "cwd" not in row["_metadata"] for row in batch.rows)


def test_skip_folders_match_the_project_basename_the_preview_lists(cursors) -> None:
    batch = read("two_folders", cursors, skip_folders=("secretproj",))
    assert [row["content"] for row in batch.rows] == ["Widget work", "Sure."]
    assert {row["thread_id"] for row in batch.rows} == {uid(0x10D)}
    assert batch.ledger.as_dict()["dropped"] == {"skipped_folder": 1}
    assert batch.scanned_files == 1 and batch.files_touched == 1
    secret_file = FIXTURES / "two_folders" / "-Users-fake-dev-secretproj" / f"{uid(0x10E)}.jsonl"
    assert cursors.get(SOURCE_ID, str(secret_file)) is None


def test_session_path_honours_a_basename_skip(cursors) -> None:
    skipped = FIXTURES / "two_folders" / "-Users-fake-dev-secretproj" / f"{uid(0x10E)}.jsonl"
    batch = read_claude_code_batch(
        root_path=FIXTURES / "two_folders", cursors=cursors, session_path=str(skipped),
        skip_folders=("secretproj",), hostnames=(), usernames=(),
    )
    assert batch.rows == []
    assert batch.ledger.as_dict()["dropped"] == {"skipped_folder": 1}
    assert cursors.get(SOURCE_ID, str(skipped)) is None


def _rewrite_cwd(root: Path, slug: str, cwd) -> None:
    """Rewrite every record's ``cwd`` under ``root/slug`` (``None`` drops the field)."""
    for path in (root / slug).glob("*.jsonl"):
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if cwd is None:
                record.pop("cwd", None)
            else:
                record["cwd"] = cwd
            lines.append(json.dumps(record))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_key_is_null_when_no_record_names_a_cwd(cursors, tmp_path) -> None:
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    _rewrite_cwd(root, "-Users-fake-dev-widget", None)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert_record_shape(batch.rows)
    keys = {row["thread_id"]: row["project_key"] for row in batch.rows}
    assert keys == {uid(0x10D): None, uid(0x10E): "secretproj"}
    # Without a basename the slug forms still skip it; nothing else does.
    again = read_claude_code_batch(root_path=root, cursors=FileCursorStore(sqlite3.connect(":memory:")),
                                   skip_folders=("widget",), hostnames=(), usernames=())
    assert {row["thread_id"] for row in again.rows} == {uid(0x10D), uid(0x10E)}
    slugged = read_claude_code_batch(root_path=root, cursors=FileCursorStore(sqlite3.connect(":memory:")),
                                     skip_folders=("-~-dev-widget",), hostnames=(), usernames=())
    assert {row["thread_id"] for row in slugged.rows} == {uid(0x10E)}


@pytest.mark.parametrize("cwd", ["/Users/fake", "/home/fake/", "C:\\Users\\fake", "/Users/John Doe", "C:\\Users\\John Doe"])
def test_a_session_run_from_the_home_folder_has_no_project_key_and_never_the_login_name(cursors, tmp_path, cwd) -> None:
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    _rewrite_cwd(root, "-Users-fake-dev-widget", cwd)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert_record_shape(batch.rows)
    keys = {row["thread_id"]: row["project_key"] for row in batch.rows}
    assert keys == {uid(0x10D): None, uid(0x10E): "secretproj"}
    blob = json.dumps([row["project_key"] for row in batch.rows])
    assert "fake" not in blob and "Doe" not in blob and "~" not in blob


def test_a_home_folder_under_a_root_the_scrub_does_not_know_still_gives_no_key(cursors, tmp_path) -> None:
    """WSL keeps homes under /mnt/c/Users; the path rules do not fire there, the login name does."""
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    _rewrite_cwd(root, "-Users-fake-dev-widget", "/mnt/c/Users/fake")
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=("fake",))
    keys = {row["thread_id"]: row["project_key"] for row in batch.rows}
    assert keys == {uid(0x10D): None, uid(0x10E): "secretproj"}
    # Without the login name the reader has nothing to recognise it by: the rule is only as good as its input.
    unaware = read_claude_code_batch(root_path=root, cursors=FileCursorStore(sqlite3.connect(":memory:")),
                                     hostnames=(), usernames=())
    assert {row["project_key"] for row in unaware.rows if row["thread_id"] == uid(0x10D)} == {"fake"}


@pytest.mark.parametrize("cwd,expected", [
    ("/Users/fake/dev/my-app/", "my-app"),
    ("C:\\Users\\fake\\dev\\my-app", "my-app"),
    ("/Users/Shared/dev/shared-app", "shared-app"),
])
def test_project_key_is_the_last_path_segment_on_either_separator(cursors, tmp_path, cwd, expected) -> None:
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    _rewrite_cwd(root, "-Users-fake-dev-widget", cwd)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert {row["project_key"] for row in batch.rows if row["thread_id"] == uid(0x10D)} == {expected}
    skipped = read_claude_code_batch(root_path=root, cursors=FileCursorStore(sqlite3.connect(":memory:")),
                                     skip_folders=(expected,), hostnames=(), usernames=())
    assert {row["thread_id"] for row in skipped.rows} == {uid(0x10E)}
    assert skipped.ledger.as_dict()["dropped"] == {"skipped_folder": 1}


def test_the_folder_key_skips_unusable_cwd_values_and_keeps_looking(cursors, tmp_path) -> None:
    """A null, blank or non-string ``cwd`` ahead of the real one does not end the search with None."""
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    path = next((root / "-Users-fake-dev-widget").glob("*.jsonl"))
    body = path.read_text(encoding="utf-8")
    head = "\n".join(json.dumps(record) for record in (
        {"type": "summary", "summary": "Widget", "cwd": None},
        {"type": "file-history-snapshot", "cwd": ""},
        {"type": "progress", "cwd": 7},
    ))
    path.write_text(head + "\n" + body, encoding="utf-8")
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert {row["project_key"] for row in batch.rows if row["thread_id"] == uid(0x10D)} == {"widget"}


def test_a_hooks_path_outside_the_root_is_dropped_before_any_file_is_read(cursors, tmp_path, monkeypatch) -> None:
    """Security re-check N4 still holds with the peek: an outside-root ``transcript_path`` is never opened."""
    from topos.ingestion.sources import claude_code_reader as module

    outside = tmp_path / "elsewhere" / "-Users-fake-dev-widget" / f"{uid(0x10D)}.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text(json.dumps({"type": "user", "cwd": "/Users/fake/dev/widget"}) + "\n", encoding="utf-8")
    peeked: list = []
    monkeypatch.setattr(module, "_folder_project_key", lambda files, usernames: peeked.append(list(files)))
    batch = read_claude_code_batch(
        root_path=FIXTURES / "two_folders", cursors=cursors, session_path=str(outside), hostnames=(), usernames=(),
    )
    assert batch.rows == [] and peeked == []
    assert batch.ledger.as_dict()["dropped"] == {"outside_root": 1}


def test_the_folder_key_comes_from_the_first_record_that_names_a_cwd(cursors, tmp_path) -> None:
    """A summary line first, then records: the key is still the folder's basename."""
    root = tmp_path / "two_folders"
    shutil.copytree(FIXTURES / "two_folders", root)
    path = next((root / "-Users-fake-dev-widget").glob("*.jsonl"))
    body = path.read_text(encoding="utf-8")
    path.write_text(json.dumps({"type": "summary", "summary": "Widget", "leafUuid": uid(0xD2)}) + "\n" + body,
                    encoding="utf-8")
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert {row["project_key"] for row in batch.rows if row["thread_id"] == uid(0x10D)} == {"widget"}


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
        f"claude_code:{uid(n)}" for n in (0xA1, 0xA2, 0xA5, 0xA6, 0xA7)
    ]
    assert by_uuid(batch.rows, 0xA2)["content"] == "Let me ask your Topos."
    assert by_uuid(batch.rows, 0xA7)["content"] == "Last week was the tray icon.\n\nAnything else?"
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    # A reply is cut at the taint instant: the narration before the Topos call stays shareable on its own
    # row, everything after it is excluded; 0xA7 and 0xA8 are one reply.
    assert excluded == {
        f"claude_code:{uid(0xA1)}": False,
        f"claude_code:{uid(0xA2)}": False,
        f"claude_code:{uid(0xA5)}": True,
        f"claude_code:{uid(0xA6)}": False,
        f"claude_code:{uid(0xA7)}": True,
    }
    assistant_after = [r for r in batch.rows if r["role"] == "assistant" and r["_metadata"]["share_excluded"]]
    assert len(assistant_after) == 2
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 2
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
    # A reply is cut at the taint instant: the narration before the Topos call stays shareable on its own
    # row, everything after it is excluded; 0xA7 and 0xA8 are one reply.
    assert excluded == {
        f"claude_code:{uid(0xA1)}": False,
        f"claude_code:{uid(0xA2)}": False,
        f"claude_code:{uid(0xA5)}": True,
        f"claude_code:{uid(0xA6)}": False,
        f"claude_code:{uid(0xA7)}": True,
    }
    assert batch.ledger.as_dict()["tainted_turns"] == 2


def test_a_health_check_on_the_socket_is_a_system_touch_recorded_without_excluding(cursors) -> None:
    """Owner ruling 20 Oct 2026: the fixture's ``curl --unix-socket ~/.topos/engine.sock …/healthcheck`` reads no
    owner data, so nothing after it is excluded — but the touch is recorded and counted."""
    batch = read("bash_touches_topos", cursors)
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    # Nothing excludes, so the narration on both sides of the health check is ONE reply (one row per turn).
    assert excluded == {
        f"claude_code:{uid(0xB1)}": False,
        f"claude_code:{uid(0xB2)}": False,
    }
    assert by_uuid(batch.rows, 0xB2)["content"] == "Checking.\n\nThe node answers.\n\nShall I continue?"
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 0
    assert ledger["topos_system_touches"] == 1 and ledger["topos_reads"] == 0
    [touch] = batch.ledger.touches
    assert (touch["tool"], touch["session_id"], touch["kind"], touch["surface"]) == (
        "claude_code", uid(0x10B), "system", "topos_socket_admin")
    assert touch["touched_at"].startswith("2026-")


def test_bash_reading_the_topos_database_taints_the_session(cursors, tmp_path) -> None:
    root = tmp_path / "bash_reads_topos"
    shutil.copytree(FIXTURES / "bash_touches_topos", root)
    transcript = root / SLUG / f"{uid(0x10B)}.jsonl"
    text = transcript.read_text()
    assert "http://localhost/healthcheck" in text
    transcript.write_text(text.replace("http://localhost/healthcheck", "http://localhost/api/local/owner/query"))
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=("fakebox-7",), usernames=("fake",))
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0xB1)}": False,
        f"claude_code:{uid(0xB2)}": False,
        f"claude_code:{uid(0xB5)}": True,
    }
    assert by_uuid(batch.rows, 0xB5)["content"] == "The node answers.\n\nShall I continue?"
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 1  # B5 and B6 are one reply after the read
    assert ledger["topos_reads"] == 1 and ledger["topos_system_touches"] == 0
    assert [t["surface"] for t in batch.ledger.touches] == ["topos_socket_data"]


def test_subagent_only_taint_marks_the_parent_session(cursors) -> None:
    batch = read("subagent_taint", cursors)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x81, 0x82, 0x85)]
    assert not any(row["id"] == f"claude_code:{uid(0x94)}" for row in batch.rows)
    assert not any("Found 2 mails" in row["content"] for row in batch.rows)
    excluded = {row["id"]: row["_metadata"]["share_excluded"] for row in batch.rows}
    assert excluded == {
        f"claude_code:{uid(0x81)}": False,
        f"claude_code:{uid(0x82)}": False,
        f"claude_code:{uid(0x85)}": True,
    }
    ledger = batch.ledger.as_dict()
    assert ledger["tainted_turns"] == 1
    assert ledger["subagent_files_scanned"] == 1
    assert ledger["dropped"] == {"tool_result": 1, "tool_use": 1}


def test_taint_persists_across_runs_so_later_turns_stay_excluded(cursors, tmp_path) -> None:
    root = tmp_path / "subagent_taint"
    shutil.copytree(FIXTURES / "subagent_taint", root)
    first = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert first.ledger.as_dict()["tainted_turns"] == 1
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
    assert second.rows[0]["_metadata"]["turn_index"] == 3
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
    # The session must lie under the tool's root (security re-check N4): the root is its own store, and the
    # other sessions there stay unread because session_path names exactly one file.
    batch = read_claude_code_batch(
        root_path=FIXTURES / "subagent_taint", cursors=cursors, session_path=str(path), hostnames=(), usernames=(),
    )
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x81, 0x82, 0x85)]
    assert batch.ledger.as_dict()["tainted_turns"] == 1
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


def test_a_tool_call_taints_by_any_input_that_names_a_topos_surface_not_only_a_shell_command() -> None:
    """Security review F6: a Read/Grep/Edit of ~/.topos/… is a Topos touch as much as `topos-node status` is."""
    from topos.ingestion.sources.claude_code_reader import _tool_use_taint

    assert _tool_use_taint({"type": "tool_use", "name": "Read", "input": {"file_path": "/Users/someone/.topos/database.db"}})
    assert _tool_use_taint({"type": "tool_use", "name": "Grep", "input": {"pattern": "key", "path": "/Users/someone/.topos/"}})
    # `topos-node status` administers the node and returns no owner data: a system touch (owner ruling 20 Oct 2026).
    assert not _tool_use_taint({"type": "tool_use", "name": "Bash", "input": {"command": "topos-node status --json"}})
    assert _tool_use_taint({"type": "tool_use", "name": "Bash", "input": {"command": "topos-node mcp --stdio"}})
    assert _tool_use_taint({"type": "tool_use", "name": "mcp__plugin_topos__query_scope", "input": {"scope_id": "messages:read"}})
    assert not _tool_use_taint({"type": "tool_use", "name": "Read", "input": {"file_path": "/Users/someone/project/README.md"}})
    assert not _tool_use_taint({"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}})
    assert not _tool_use_taint({"type": "tool_use", "name": "Edit", "input": None})
    # an absolute ~/.topos path with no trailing slash is the same surface (security re-check N6); listing it
    # is a system touch, reading through it is a read
    assert not _tool_use_taint({"type": "tool_use", "name": "Bash", "input": {"command": "ls /Users/someone/.topos"}})
    assert _tool_use_taint({"type": "tool_use", "name": "Bash", "input": {"command": "cat /Users/someone/.topos/.env"}})
    assert _tool_use_taint({"type": "tool_use", "name": "Bash",
                            "input": {"command": "grep -r token /Users/someone/.topos"}})


def test_a_hooks_session_path_outside_the_tools_root_reads_nothing(cursors, tmp_path) -> None:
    """The sync door takes session_path from any socket caller (security re-check N4): a path outside the
    tool's own folder is dropped and counted, never read as a transcript."""
    outside = tmp_path / "elsewhere" / "sess-x.jsonl"
    outside.parent.mkdir()
    outside.write_text('{"type": "user", "uuid": "u-1", "message": {"content": "hi"}, "timestamp": "2026-10-01T09:00:00Z"}\n')
    batch = read_claude_code_batch(root_path=FIXTURES / "plain", cursors=cursors, session_path=str(outside),
                                   hostnames=(), usernames=())
    assert batch.rows == [] and batch.scanned_files == 0
    assert batch.ledger.as_dict()["dropped_files"] == {"outside_root": 1}


def test_output_only_tools_are_judged_by_their_target_path_not_their_text() -> None:
    """Writing a note that mentions ~/.topos/.env, or briefing a sub-agent about the database, reads nothing of
    Topos (the sub-agent's own calls are scanned from its transcript). The target path still counts (F6)."""
    from topos.ingestion.sources.claude_code_reader import _tool_use_touch

    def touch(name, inputs):
        return _tool_use_touch({"type": "tool_use", "name": name, "input": inputs})

    assert touch("Write", {"file_path": "/Users/someone/dev/NOTES.md",
                           "content": "never cat ~/.topos/.env or open ~/.topos/database.db"}) is None
    assert touch("Edit", {"file_path": "/Users/someone/dev/a.py", "old_string": "x",
                          "new_string": "DB = '~/.topos/database.db'"}) is None
    assert touch("Agent", {"prompt": "count rows in ~/.topos/database.db with sqlite3"}) is None
    assert touch("SendMessage", {"to": "peer", "message": "topos-node mcp is down"}) is None
    assert touch("Bash", {"command": "git status", "description": "Unlike ~/.topos/.env this is safe"}) is None
    # the target path still decides
    assert touch("Edit", {"file_path": "/Users/someone/.topos/.env", "old_string": "a", "new_string": "b"})[0] == "read"
    assert touch("Write", {"file_path": "/Users/someone/.topos/config.yaml", "content": "x"})[0] == "system"
    # a tool this build does not know keeps the all-inputs rule (fail closed)
    assert touch("SomeNewTool", {"note": "/Users/someone/.topos/database.db"})[0] == "read"
    # the command itself still decides for Bash
    assert touch("Bash", {"command": "sqlite3 ~/.topos/database.db .tables", "description": "count"})[0] == "read"


# ---------------------------------------------------------------------------
# One row per reply: the narration of one agentic turn is one row
# ---------------------------------------------------------------------------


AGENTIC_REPLY = (
    "Let me look at the test first.\n\n"
    "The test sleeps instead of waiting for the upload.\n\n"
    "Fixed: it now waits on the upload event.\n\n"
    "The test passed ten runs in a row."
)


def test_one_agentic_turn_is_one_row_across_its_tool_calls_and_meta_records(cursors) -> None:
    batch = read("agentic_reply", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(n)}" for n in (0x1B1, 0x1B2, 0x1BD, 0x1BE, 0x1C2)]
    assert roles(batch.rows) == ["user", "assistant", "user", "assistant", "assistant"]
    # Id and stamp are the reply's FIRST text record's; text is every text block in order, nothing else.
    reply = by_uuid(batch.rows, 0x1B2)
    assert reply["content"] == AGENTIC_REPLY
    assert reply["created_at"] == "2026-10-01T09:00:00.250Z"
    assert "sleep(0.1)" not in reply["content"] and "It sleeps instead" not in reply["content"]
    assert [row["_metadata"]["turn_index"] for row in batch.rows] == [0, 1, 2, 3, 4]
    ledger = batch.ledger.as_dict()
    assert ledger["merged_blocks"] == 4
    assert ledger["turns_emitted"] == 5
    assert ledger["dropped"] == {"system": 1, "thinking": 1, "tool_result": 3, "tool_use": 3}
    assert ledger["meta_records"] == 3
    assert ledger["message_nodes"] == 20


def test_a_prompt_typed_mid_turn_splits_the_reply(cursors) -> None:
    # A queued_command attachment is a prompt delivered while the agent works: what comes after answers it.
    batch = read("agentic_reply", cursors)
    assert by_uuid(batch.rows, 0x1BE)["content"] == "Checking the lint output."
    second = by_uuid(batch.rows, 0x1C2)
    assert second["content"] == "Line 12 was too long; it is wrapped now.\n\nAnd the README typo is fixed."
    assert second["created_at"] == "2026-10-01T09:00:04.250Z"


def test_a_finished_reply_on_a_file_just_written_is_emitted_at_once(cursors, tmp_path) -> None:
    # The hook case: the Stop hook syncs the instant the turn ends; end_turn closes the reply.
    root = tmp_path / "agentic_reply"
    shutil.copytree(FIXTURES / "agentic_reply", root)
    path = root / SLUG / f"{uid(0x117)}.jsonl"
    os.utime(path, None)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert by_uuid(batch.rows, 0x1C2)["content"].endswith("And the README typo is fixed.")
    assert cursors.get(SOURCE_ID, str(path)).offset == path.stat().st_size


def _open_reply_root(tmp_path, *, age_seconds: float, tail_lines: int = 0):
    root = tmp_path / "open_reply"
    shutil.copytree(FIXTURES / "open_reply", root)
    session = uid(0x118)
    path = root / SLUG / f"{session}.jsonl"
    if tail_lines:
        tail = (FIXTURES / "open_reply_tail" / SLUG / f"{session}.jsonl").read_bytes().splitlines(keepends=True)
        with path.open("ab") as handle:
            handle.write(b"".join(tail[:tail_lines]))
    stamp = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(path, (stamp, stamp))
    return root, path


def test_a_reply_still_running_is_held_back_and_merged_on_the_next_run(cursors, tmp_path) -> None:
    root, path = _open_reply_root(tmp_path, age_seconds=5)
    first_line = len(path.read_bytes().splitlines(keepends=True)[0])
    first = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in first.rows] == [f"claude_code:{uid(0x1D1)}"]
    # The cursor stops where the open reply starts, and nothing read past it is counted yet.
    assert cursors.get(SOURCE_ID, str(path)).offset == first_line
    assert cursors.get(SOURCE_ID, str(path)).record_count == 1
    assert first.ledger.as_dict()["dropped"] == {}
    assert first.ledger.as_dict()["message_nodes"] == 1
    tail = (FIXTURES / "open_reply_tail" / SLUG / f"{uid(0x118)}.jsonl").read_bytes()
    with path.open("ab") as handle:
        handle.write(tail)
    second = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in second.rows] == [f"claude_code:{uid(0x1D2)}"]
    row = second.rows[0]
    assert row["content"] == "Starting the slow suite.\n\nAll 12 slow tests passed."
    assert row["created_at"] == "2026-10-01T09:00:00.250Z"
    assert row["_metadata"]["turn_index"] == 1
    assert second.ledger.as_dict()["dropped"] == {"tool_result": 1, "tool_use": 1}
    assert second.ledger.as_dict()["merged_blocks"] == 1
    assert cursors.get(SOURCE_ID, str(path)).offset == path.stat().st_size


def test_a_held_reply_with_nothing_new_reports_the_file_untouched(cursors, tmp_path) -> None:
    # The runner loops while a batch reports touched files: a held reply must not keep it spinning.
    root, path = _open_reply_root(tmp_path, age_seconds=5)
    read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    again = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert again.rows == [] and again.files_touched == 0


def test_a_reply_waiting_on_a_tool_result_just_written_is_still_held(cursors, tmp_path) -> None:
    root, path = _open_reply_root(tmp_path, age_seconds=5, tail_lines=1)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(0x1D1)}"]
    assert cursors.get(SOURCE_ID, str(path)).offset == len(path.read_bytes().splitlines(keepends=True)[0])


def test_a_trailing_tool_result_alone_keeps_the_reply_open(cursors, tmp_path) -> None:
    # The last assistant record carries no stop_reason; only the tool result after it says a call is pending.
    root, path = _open_reply_root(tmp_path, age_seconds=5, tail_lines=1)
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[2])
    record["message"].pop("stop_reason")
    lines[2] = (json.dumps(record) + "\n").encode()
    path.write_bytes(b"".join(lines))
    os.utime(path, None)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(0x1D1)}"]


def test_an_open_reply_in_a_file_quiet_for_an_hour_is_emitted(cursors, tmp_path) -> None:
    # A session closed or crashed mid-tool never writes the end of its turn; the reply is not held forever.
    root, path = _open_reply_root(tmp_path, age_seconds=2 * 3600)
    batch = read_claude_code_batch(root_path=root, cursors=cursors, hostnames=(), usernames=())
    assert [row["id"] for row in batch.rows] == [f"claude_code:{uid(0x1D1)}", f"claude_code:{uid(0x1D2)}"]
    assert batch.rows[1]["content"] == "Starting the slow suite."
    assert batch.ledger.as_dict()["dropped"] == {"tool_use": 1}
    assert cursors.get(SOURCE_ID, str(path)).offset == path.stat().st_size


# ---------------------------------------------------------------------------
# Harness spans after typed text (§2.3)
# ---------------------------------------------------------------------------


def test_trailing_harness_spans_are_stripped_and_counted_but_the_persons_own_text_is_kept(cursors) -> None:
    batch = read("harness_trailing", cursors)
    assert_record_shape(batch.rows)
    users = [row for row in batch.rows if row["role"] == "user"]
    assert [row["id"] for row in users] == [
        f"claude_code:{uid(n)}" for n in (0x1E1, 0x1E3, 0x1E4, 0x1E5, 0x1E6, 0x1E7, 0x1E8, 0x1E9)
    ]
    assert [row["content"] for row in users] == [
        "Fix the flaky test",
        "Why does `<command-name>` show up in my rows?",
        "Keep this:\n<system-reminder>pasted in the middle</system-reminder>\nand this line too",
        "Ship it",
        "Two notes",
        "Look here",
        # A span named inline at the end is not on a line of its own: the person's words.
        "Is it fine to end a note with <system-reminder>x</system-reminder>",
        # Only the span that ends the text goes; typed text between two spans is never swallowed.
        "a\n<system-reminder>1</system-reminder>\nkeep me",
    ]
    assert {row["_metadata"]["conversation_title"] for row in batch.rows} == {"Fix the flaky test"}
    ledger = batch.ledger.as_dict()
    assert ledger["harness_spans_stripped"] == 6
    assert ledger["dropped"] == {}
    assert ledger["turns_emitted"] == 9


def test_a_system_record_ends_a_reply_and_sub_agent_records_do_not(cursors) -> None:
    batch = read("reply_edges", cursors)
    assert_record_shape(batch.rows)
    assert [row["id"] for row in batch.rows] == [
        f"claude_code:{uid(n)}" for n in (0x201, 0x202, 0x204, 0x205, 0x206, 0x20C, 0x20D, 0x20E)
    ]
    assert by_uuid(batch.rows, 0x202)["content"] == "Looking at the deploy log."
    assert by_uuid(batch.rows, 0x204)["content"] == "Retried: the deploy is green."
    assert by_uuid(batch.rows, 0x206)["content"] == "Drafting them with a helper.\n\nThe notes are in RELEASE.md."
    assert not any("Helper draft" in row["content"] for row in batch.rows)


def test_text_beside_a_topos_call_never_drags_earlier_narration_out_of_sharing(cursors) -> None:
    # The record holding the call's own text is excluded (its stamp is the taint instant); the reply text
    # before it keeps a shareable row of its own, and no row mixes the two.
    batch = read("reply_edges", cursors)
    before = by_uuid(batch.rows, 0x20D)
    after = by_uuid(batch.rows, 0x20E)
    assert (before["content"], before["_metadata"]["share_excluded"]) == ("Let me check.", False)
    assert (after["content"], after["_metadata"]["share_excluded"]) == (
        "Asking your Topos now.\n\nYou shipped the uploader.", True,
    )
    assert after["created_at"] == "2026-10-01T09:00:03.250Z"
    assert batch.ledger.as_dict()["tainted_turns"] == 1
