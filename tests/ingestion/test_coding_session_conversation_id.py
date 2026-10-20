"""One coding session is one conversation, named ``<tool>:<session_id>``, whatever else the process installed.

20 Oct 2026, the owner's node: every Claude Code message ingested by 1.5.13 was filed under ``codex_sessions:<session>``
and every one ingested by the next build under ``claude_code_sessions:<session>``, so sessions that spanned the restart
were split in two. The three coding sources share one mapper id, ``ai_coding_tool``, registered in code in the ai_chat
mapper registry. A runtime install looked for it only in the legacy registry, did not find it, and registered a shim
bound to the installing source's id over it. The first coding source a process installed decided every coding
source's prefix until the next restart (``sources/runtime_install.py``).

These run the real parsers, the real mapper and the real install path (``ai_tool_fixtures.arm(fakes=False)``). The
reader is a fake; the records are invented.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.ingestion.ai_tool_fixtures import DATASET, FakeReader, arm, build_node, chat_rows, enable, record

CLAUDE = "claude_code_sessions"
CODEX = "codex_sessions"
SESSION = "sess-0001"


def _turns(*uuids_and_times):
    return [
        record("claude_code", uuid, SESSION, "user" if i % 2 == 0 else "assistant", f"turn {uuid}", when=when,
               turn_index=i)
        for i, (uuid, when) in enumerate(uuids_and_times)
    ]


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    from topos.engine import disk_space

    monkeypatch.setattr(disk_space, "free_bytes", lambda path=None: 1_000_000_000_000)
    conn = build_node(tmp_path / "node.db")
    yield conn
    conn.close()


@pytest.fixture
def restart():
    """Reset what a node restart resets: the in-process registries a runtime install writes. The database stays."""
    from topos.canonicalization.mappers import MAPPER_REGISTRY as LEGACY_MAPPERS
    from topos.ingestion.parsers import PARSER_REGISTRY
    from topos.sources import install_service
    from topos.sources.registry import REGISTRY
    from topos.storage.canonical.ai_chat import mapper as ai_chat_mapper
    from topos.storage.canonical.ai_chat.coding_tool_mapper import CodingToolToAIChatMapper

    registries = (REGISTRY, PARSER_REGISTRY, LEGACY_MAPPERS, ai_chat_mapper._MAPPER_REGISTRY,
                  install_service._ACTIVE_HANDLES)
    saved = [(registry, dict(registry)) for registry in registries]

    def _restart() -> None:
        for registry, snapshot in saved:
            registry.clear()
            registry.update(snapshot)
        # As a fresh import leaves them, whatever an earlier test in this process installed.
        LEGACY_MAPPERS.pop("ai_coding_tool", None)
        ai_chat_mapper._MAPPER_REGISTRY["ai_coding_tool"] = CodingToolToAIChatMapper
        install_service._ACTIVE_HANDLES.clear()

    _restart()
    yield _restart
    _restart()


def _conversations(conn: sqlite3.Connection):
    return [tuple(r) for r in conn.execute(
        "SELECT conversation_id, source_id FROM ai_chat_conversations ORDER BY conversation_id").fetchall()]


def _sync_claude_code():
    from topos.ingestion.ai_tool_sync import run_claude_code_sync

    result = run_claude_code_sync(DATASET)
    assert result["status"] == "ok", result
    return result


def test_a_claude_code_write_after_a_codex_install_is_filed_under_claude_code(node, monkeypatch, restart) -> None:
    arm(monkeypatch, node, reader=FakeReader(_turns(("u1", "2026-10-20T10:00:00.000Z"),
                                                    ("u2", "2026-10-20T10:00:01.000Z"))), fakes=False)
    enable(node, CODEX)   # the first coding source this process installs
    enable(node, CLAUDE)
    _sync_claude_code()

    assert [(r["message_id"], r["conversation_id"]) for r in chat_rows(node)] == [
        ("claude_code:u1", "claude_code:sess-0001"),
        ("claude_code:u2", "claude_code:sess-0001"),
    ]
    assert _conversations(node) == [("claude_code:sess-0001", CLAUDE)]


def test_one_session_is_one_conversation_across_a_restart_that_installs_in_another_order(
    node, monkeypatch, restart,
) -> None:
    from topos.ingestion import ai_tool_sync as lane
    from topos.sources.install_service import rehydrate_active_installs_runtime

    arm(monkeypatch, node, reader=FakeReader(_turns(("u1", "2026-10-20T10:00:00.000Z"),
                                                    ("u2", "2026-10-20T10:00:01.000Z"))), fakes=False)
    enable(node, CODEX)
    enable(node, CLAUDE)
    _sync_claude_code()

    restart()
    # The next process brings the installs back Claude Code first: on 1.5.13 that alone moved the prefix.
    rehydrate_active_installs_runtime(source_id=CLAUDE)
    rehydrate_active_installs_runtime()
    later = FakeReader([record("claude_code", "u3", SESSION, "user", "turn u3", when="2026-10-20T11:00:00.000Z",
                               turn_index=2),
                        record("claude_code", "u4", SESSION, "assistant", "turn u4", when="2026-10-20T11:00:01.000Z",
                               turn_index=3)])
    monkeypatch.setattr(lane, "_reader_for", lambda tool: later)
    _sync_claude_code()

    assert {r["conversation_id"] for r in chat_rows(node)} == {"claude_code:sess-0001"}
    assert len(chat_rows(node)) == 4
    assert _conversations(node) == [("claude_code:sess-0001", CLAUDE)]


def test_a_runtime_install_of_a_coding_source_keeps_the_bundled_coding_mapper(restart) -> None:
    from topos.sources.registry import BUNDLED_REGISTRY
    from topos.sources.runtime_install import install_source_definition
    from topos.storage.canonical.ai_chat.coding_tool_mapper import CodingToolToAIChatMapper
    from topos.storage.canonical.ai_chat.mapper import get_mapper

    install_source_definition(BUNDLED_REGISTRY[CODEX].to_dict())
    install_source_definition(BUNDLED_REGISTRY[CLAUDE].to_dict())

    assert type(get_mapper("ai_coding_tool")) is CodingToolToAIChatMapper


def test_a_runtime_mapper_two_sources_share_names_each_row_after_its_own_source(restart) -> None:
    """Defence in depth for any shared runtime mapper id: the conversation prefix is the row's source, not the
    source that happened to install the shim."""
    from topos.sources.runtime_install import install_source_definition
    from topos.storage.canonical.ai_chat.mapper import get_mapper

    for source_id in ("alpha_chat_test", "beta_chat_test"):
        install_source_definition({
            "source_id": source_id,
            "display_name": source_id,
            "source_type": "ui_stream",
            "schema_id": f"{source_id}.v1",
            "parser_id": f"{source_id}.v1",
            "canonical_group_id": "ai_messages",
            "canonical_mapper_id": "shared_runtime_chat_mapper_test",
            "default_scope_id": "ai_conversations",
            "allowed_scope_ids": ["ai_conversations:read"],
        })

    mapper = get_mapper("shared_runtime_chat_mapper_test")
    assert mapper.extract_conversation_id({"source_id": "beta_chat_test", "thread_id": "t-1"}) == "beta_chat_test:t-1"
    assert mapper.extract_conversation_id({"source_id": "alpha_chat_test", "thread_id": "t-1"}) == "alpha_chat_test:t-1"


@pytest.mark.parametrize(
    ("staging", "expected"),
    [
        # The source the row is written under names the tool, whatever else the record says.
        ({"source_id": "claude_code_sessions", "thread_id": "s-1", "message_id": "claude_code:m"}, "claude_code:s-1"),
        ({"source_id": "codex_sessions", "thread_id": "s-1", "message_id": "codex:m", "_metadata": {}}, "codex:s-1"),
        ({"source_id": "cursor_sessions", "thread_id": "s-1", "_metadata": {"tool": "cursor"}}, "cursor:s-1"),
        # The source wins over a record that names another tool.
        ({"source_id": "claude_code_sessions", "thread_id": "s-1", "_metadata": {"tool": "codex"}, "message_id": "codex:m"},
         "claude_code:s-1"),
        # No source on the record (a direct mapper call): the record's own tool, as before.
        ({"thread_id": "s-1", "_metadata": {"tool": "codex"}}, "codex:s-1"),
    ],
)
def test_the_mapper_names_a_coding_conversation_tool_colon_session(staging, expected) -> None:
    from topos.storage.canonical.ai_chat.coding_tool_mapper import CodingToolToAIChatMapper

    assert CodingToolToAIChatMapper().extract_conversation_id(staging) == expected


@pytest.mark.parametrize(
    ("source_id", "stored", "expected"),
    [
        ("claude_code_sessions", "codex_sessions:5d9c", "claude_code:5d9c"),
        ("claude_code_sessions", "claude_code_sessions:5d9c", "claude_code:5d9c"),
        ("claude_code_sessions", "claude_code:5d9c", "claude_code:5d9c"),
        ("codex_sessions", "claude_code_sessions:5d9c", "codex:5d9c"),
        ("cursor_sessions", "ai_coding_tool:5d9c", "cursor:5d9c"),
        # The mapper's no-thread fallback names the dataset, which has colons of its own.
        ("codex_sessions", "codex_sessions:owner:topos:t1", "codex:owner:topos:t1"),
        # Not a shape any coding writer produced: left alone.
        ("claude_code_sessions", "chatgpt:5d9c", None),
        ("claude_code_sessions", "5d9c", None),
        ("claude_code_sessions", "codex_sessions:", None),
        ("chatgpt_export", "codex_sessions:5d9c", None),
    ],
)
def test_the_canonical_form_of_a_stored_coding_conversation_id(source_id, stored, expected) -> None:
    from topos.storage.canonical.ai_chat.coding_tool_mapper import canonical_coding_conversation_id

    assert canonical_coding_conversation_id(source_id, stored) == expected
