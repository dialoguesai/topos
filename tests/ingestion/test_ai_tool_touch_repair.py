"""Re-judging stored coding-session replies under the read / system-touch rule (owner ruling 20 Oct 2026).

Synthetic transcripts and an invented node database only. A dry run changes nothing; ``apply`` clears the flag
only where the reply's own session shows no READ of Topos before it, records every touch, moves Claude Code's
"excluded from" marker, re-projects the cleared rows and hands them to the catch-up. Everything else stays kept.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ingestion.ai_tool_fixtures import build_node

SOURCE = "claude_code_sessions"
SLUG = "-Users-fake-dev-widget"
S_SYSTEM = "aaaaaaaa-0000-4000-8000-000000000001"   # only a health check: every reply comes back
S_READ = "aaaaaaaa-0000-4000-8000-000000000002"     # a query_scope call: replies after it stay kept
S_GONE = "aaaaaaaa-0000-4000-8000-000000000003"     # transcript deleted: kept, nothing to prove it
S_SUB = "aaaaaaaa-0000-4000-8000-000000000004"      # the read happened in a sub-agent


def _ts(minute: int) -> str:
    return f"2026-10-14T10:{minute:02d}:00.000Z"


def _tool_use(minute: int, tool_id: str, name: str, inputs: dict) -> dict:
    return {"type": "assistant", "uuid": f"rec-{tool_id}", "timestamp": _ts(minute), "isSidechain": False,
            "message": {"id": f"msg-{tool_id}", "role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inputs}]}}


def _write(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "projects"
    folder = base / SLUG
    _write(folder / f"{S_SYSTEM}.jsonl", [
        _tool_use(1, "toolu_health", "Bash",
                  {"command": "curl -s --unix-socket ~/.topos/engine.sock http://localhost/healthcheck"}),
        _tool_use(2, "toolu_status", "Bash", {"command": "topos-node status --json"}),
    ])
    _write(folder / f"{S_READ}.jsonl", [
        _tool_use(5, "toolu_query", "mcp__plugin_topos_topos__query_scope", {"scope_id": "messages:read"}),
    ])
    _write(folder / f"{S_SUB}.jsonl", [])
    _write(folder / S_SUB / "subagents" / "agent-1.jsonl", [
        _tool_use(7, "toolu_sub_read", "Read", {"file_path": "/Users/fake/.topos/database.db"}),
    ])
    return base


def _insert(conn, message_id: str, session: str, minute: int, *, own_flag: bool = True) -> None:
    metadata = {"thread_id": session, "mapper_id": "coding_tool", "original_source": "coding_tool",
                "_metadata": {"tool": "claude_code", "session_id": session, "share_excluded": own_flag}}
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, metadata_json,"
        " source_id, share_excluded) VALUES (?, ?, 'assistant', ?, 'invented reply text', ?, ?, 1)",
        (message_id, f"claude_code:{session}", _ts(minute), json.dumps(metadata), SOURCE),
    )


@pytest.fixture
def node(tmp_path: Path):
    conn = build_node(tmp_path / "node.db")
    _insert(conn, "claude_code:sys-a", S_SYSTEM, 3)
    _insert(conn, "claude_code:sys-b", S_SYSTEM, 9)
    _insert(conn, "claude_code:read-before", S_READ, 4)
    _insert(conn, "claude_code:read-at", S_READ, 5)
    _insert(conn, "claude_code:read-after", S_READ, 6)
    _insert(conn, "claude_code:gone", S_GONE, 3)
    _insert(conn, "claude_code:other-session", S_SYSTEM, 4, own_flag=False)
    _insert(conn, "claude_code:sub-after", S_SUB, 8)
    _insert(conn, "claude_code:sub-before", S_SUB, 6)
    conn.commit()
    yield conn
    conn.close()


def _flags(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT message_id, share_excluded FROM ai_chat_messages")}


def test_a_dry_run_counts_and_changes_nothing(node, root) -> None:
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches

    before = _flags(node)
    report = reclassify_touches(node, SOURCE, root=root)
    assert report == {
        "source_id": SOURCE, "applied": False, "status": "ok", "flagged_rows": 9, "sessions_scanned": 3,
        "sessions_with_reads": 2, "topos_reads": 2, "topos_system_touches": 2, "would_clear": 4,
        "kept_after_a_read": 3, "kept_no_transcript": 1, "kept_flagged_by_another_session": 1, "kept_undated": 0,
    }
    assert _flags(node) == before
    tables = {r[0] for r in node.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ai_coding_topos_touches" not in tables or node.execute(
        "SELECT COUNT(*) FROM ai_coding_topos_touches").fetchone()[0] == 0
    # Counts only: no session id, path or text in the answer.
    assert S_SYSTEM not in json.dumps(report) and "projects" not in json.dumps(report)


def test_apply_clears_only_replies_with_no_read_before_them(node, root, monkeypatch) -> None:
    import topos.ingestion.ai_tool_catchup as catchup
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches

    queued = []
    monkeypatch.setattr(catchup, "enqueue_catchup", lambda conn, sid: queued.append(sid) or ["job-1"])
    report = reclassify_touches(node, SOURCE, root=root, apply=True)
    assert report["applied"] is True and report["cleared"] == 4 and report["catchup_jobs_queued"] == 1
    assert queued == [SOURCE]
    assert _flags(node) == {
        "claude_code:sys-a": 0, "claude_code:sys-b": 0,            # system touches only
        "claude_code:read-before": 0,                              # strictly before the read
        "claude_code:read-at": 1, "claude_code:read-after": 1,     # at or after the read: kept
        "claude_code:gone": 1,                                     # no transcript to prove anything: kept
        "claude_code:other-session": 1,                            # flagged by another session's view: kept
        "claude_code:sub-before": 0, "claude_code:sub-after": 1,   # a sub-agent's read counts for the session
    }
    # The record's own metadata says so too, so the insert/metadata triggers can never set the flag again.
    meta = json.loads(node.execute("SELECT metadata_json FROM ai_chat_messages WHERE message_id='claude_code:sys-a'")
                      .fetchone()[0])
    assert meta["_metadata"]["share_excluded"] is False
    node.execute("UPDATE ai_chat_messages SET metadata_json = metadata_json WHERE message_id='claude_code:sys-a'")
    assert _flags(node)["claude_code:sys-a"] == 0
    touches = node.execute("SELECT session_id, kind, surface FROM ai_coding_topos_touches ORDER BY touched_at").fetchall()
    assert [tuple(t) for t in touches] == [
        (S_SYSTEM, "system", "topos_socket_admin"), (S_SYSTEM, "system", "topos_cli_admin"),
        (S_READ, "read", "topos_data_tool"), (S_SUB, "read", "topos_database"),
    ]
    timeline = {r[0] for r in node.execute("SELECT record_id FROM timeline WHERE canonical_table='ai_chat_messages'")}
    assert timeline == {"claude_code:sys-a", "claude_code:sys-b", "claude_code:read-before", "claude_code:sub-before"}
    # A second apply finds nothing new and records nothing twice.
    again = reclassify_touches(node, SOURCE, root=root, apply=True)
    assert again["cleared"] == 0 and again["touches_recorded"] == 0


def test_apply_moves_the_claude_code_marker_to_the_first_read_or_clears_it(node, root, monkeypatch) -> None:
    import topos.ingestion.ai_tool_catchup as catchup
    from topos.ingestion.checkpoints.file_cursors import FileCursorStore
    from topos.ingestion.sources import claude_code_reader as reader
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches

    monkeypatch.setattr(catchup, "enqueue_catchup", lambda conn, sid: [])
    store = FileCursorStore(node)
    system_key = str(root / SLUG / f"{S_SYSTEM}.jsonl")
    read_key = str(root / SLUG / f"{S_READ}.jsonl")
    # The old broad rule left markers at the health check (minute 1) and at minute 2 for the read session.
    reader._put_taint_marker(store, system_key, 1_791_972_060.0)
    reader._put_taint_marker(store, read_key, 1_791_972_120.0)
    node.commit()
    report = reclassify_touches(node, SOURCE, root=root, apply=True)
    assert report["markers_moved"] == 2
    assert reader._taint_marker(store, system_key) is None          # new replies there are shareable again
    assert reader._taint_marker(store, read_key) == pytest.approx(1_791_972_300.0)  # minute 5: the read


def test_cursor_sessions_are_not_rejudged_and_say_so(node) -> None:
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches

    report = reclassify_touches(node, "cursor_sessions")
    assert report == {"source_id": "cursor_sessions", "applied": False, "flagged_rows": 0,
                      "status": "unsupported", "would_clear": 0}


THREAD_D = "0a1b2c3d-4444-4ddd-8ddd-dddddddd0004"   # runs `topos-node status --json` at 10:15:06
THREAD_G = "0a1b2c3d-7777-4777-8777-a7a7a7a70007"   # an MCP call attributed to topos query_scope


def _insert_codex(conn, message_id: str, thread: str, stamp: str) -> None:
    metadata = {"thread_id": thread, "_metadata": {"tool": "codex", "session_id": thread, "share_excluded": True}}
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, metadata_json,"
        " source_id, share_excluded) VALUES (?, ?, 'assistant', ?, 'invented reply text', ?, 'codex_sessions', 1)",
        (message_id, f"codex:{thread}", stamp, json.dumps(metadata)),
    )


def test_codex_status_only_threads_come_back_and_read_threads_stay(tmp_path, monkeypatch) -> None:
    import shutil

    import topos.ingestion.ai_tool_catchup as catchup
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches

    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "ai_coding_tools" / "codex"
    codex_home = tmp_path / "codex_home"
    shutil.copytree(fixtures, codex_home, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    conn = build_node(tmp_path / "codex.db")
    _insert_codex(conn, "codex:msg_d_a2", THREAD_D, "2026-10-15T10:15:09.000Z")
    _insert_codex(conn, "codex:msg_d_a3", THREAD_D, "2026-10-15T10:15:10.000Z")
    g_stamp = "2099-01-01T00:00:00.000Z"  # after any read in G
    _insert_codex(conn, "codex:msg_g_late", THREAD_G, g_stamp)
    conn.commit()
    monkeypatch.setattr(catchup, "enqueue_catchup", lambda conn, sid: [])
    report = reclassify_touches(conn, "codex_sessions", root=codex_home, apply=True)
    flags = {r[0]: r[1] for r in conn.execute("SELECT message_id, share_excluded FROM ai_chat_messages")}
    assert flags == {"codex:msg_d_a2": 0, "codex:msg_d_a3": 0, "codex:msg_g_late": 1}
    assert report["cleared"] == 2 and report["kept_after_a_read"] == 1
    kinds = {(r[0], r[1]) for r in conn.execute("SELECT session_id, kind FROM ai_coding_topos_touches")}
    assert (THREAD_D, "system") in kinds and (THREAD_G, "read") in kinds
    conn.close()
