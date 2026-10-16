"""Parser subclasses, schema ids, registry entries, bundled triples and the ai_coding_tool mapper (CONTRACTS §2.1–2.2).

protects: the three schema ids resolve to bundled parsers that validate §2.2 records;
``sender_type`` is human/assistant/system by role; the mapper mints ``<tool>:<session>``
conversation ids and merges ``_metadata`` into ``metadata_json`` without any path facet;
the title reaches ``ai_chat_conversations``; the mapper is registered by importing the
parser package (the site the canonical pipeline already imports).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.ingestion.parsers import BUNDLED_PARSER_CLASSES, PARSER_REGISTRY
from topos.ingestion.parsers.ai_coding_tool import (
    ClaudeCodeTranscriptParser,
    CodexTranscriptParser,
    CursorTranscriptParser,
)
from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.sources.base import RawRecord
from topos.ingestion.validation.schema_registry import SCHEMAS, validate_schema
from topos.sources.bundled_canonical_triples import (
    BUNDLED_CANONICAL_TRIPLES,
    apply_bundled_canonical_defaults,
    infer_bundled_canonical_triple,
)
from topos.storage.canonical.ai_chat.coding_tool_mapper import (
    EXCLUDED_METADATA_KEYS,
    MAPPER_ID,
    CodingToolToAIChatMapper,
)
from topos.storage.canonical.ai_chat.mapper import get_mapper

SCHEMA_IDS = ("claude_code.transcript.v1", "codex.transcript.v1", "cursor.transcript.v1")
DATASET = "user-1:default:device1"


def record(tool: str = "claude_code", role: str = "user", **overrides) -> dict:
    base = {
        "id": f"{tool}:0000000a-0000-4000-8000-00000000000a",
        "thread_id": "sess-1",
        "role": role,
        "content": "Add a retry to the uploader",
        "created_at": "2026-10-01T09:00:00.250Z",
        "_metadata": {
            "tool": tool,
            "session_id": "sess-1",
            "turn_index": 0,
            "conversation_title": "Uploader retry",
            "origin": "keyboard",
            "share_excluded": False,
            "timestamp_source": "record",
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema ids, registry, triples
# ---------------------------------------------------------------------------


def test_three_schema_ids_are_registered_with_the_flat_record_fields() -> None:
    for schema_id in SCHEMA_IDS:
        schema = SCHEMAS[schema_id]
        assert schema["required_fields"] == ["id", "thread_id", "role", "content", "created_at"]
        assert schema["field_types"]["created_at"] is str
        assert schema["file_format"] == "jsonl"
        ok, error = validate_schema(record(schema_id.split(".")[0]), schema_id)
        assert ok and error is None


def test_parser_registry_maps_each_schema_id_to_its_bundled_parser() -> None:
    assert PARSER_REGISTRY["claude_code.transcript.v1"] is ClaudeCodeTranscriptParser
    assert PARSER_REGISTRY["codex.transcript.v1"] is CodexTranscriptParser
    assert PARSER_REGISTRY["cursor.transcript.v1"] is CursorTranscriptParser
    for cls in (ClaudeCodeTranscriptParser, CodexTranscriptParser, CursorTranscriptParser):
        assert cls in BUNDLED_PARSER_CLASSES


def test_bundled_triples_send_the_three_schema_ids_to_one_mapper_on_the_ai_messages_lane() -> None:
    for schema_id in SCHEMA_IDS:
        assert BUNDLED_CANONICAL_TRIPLES[schema_id] == ("ai_coding_tool", "ai_messages")
        assert infer_bundled_canonical_triple(schema_id=schema_id) == ("ai_coding_tool", "ai_messages")
    defn = apply_bundled_canonical_defaults(
        {
            "source_id": "claude_code_sessions",
            "display_name": "Claude Code sessions",
            "source_type": "local_sync",
            "schema_id": "claude_code.transcript.v1",
            "parser_id": "claude_code.transcript.v1",
            "canonical_group_id": "ai_messages",
        }
    )
    assert defn["canonical_mapper_id"] == "ai_coding_tool"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,tool,schema_id",
    [
        (ClaudeCodeTranscriptParser, "claude_code", "claude_code.transcript.v1"),
        (CodexTranscriptParser, "codex", "codex.transcript.v1"),
        (CursorTranscriptParser, "cursor", "cursor.transcript.v1"),
    ],
)
def test_each_parser_validates_and_parses_a_contract_record(cls, tool, schema_id) -> None:
    parser = cls(dataset_id=DATASET)
    assert parser.schema_id() == schema_id
    assert parser.tool == tool
    raw = RawRecord(record_id="ignored", payload=record(tool))
    result = parser.validate(raw)
    assert result.is_valid and result.errors == []
    normalized = parser.parse(raw)
    assert normalized.record_id == f"{tool}:0000000a-0000-4000-8000-00000000000a"
    assert normalized.payload == {
        "message_id": f"{tool}:0000000a-0000-4000-8000-00000000000a",
        "dataset_id": DATASET,
        "thread_id": "sess-1",
        "ts": "2026-10-01T09:00:00.250Z",
        "sender_type": "human",
        "content": "Add a retry to the uploader",
        "_metadata": record(tool)["_metadata"],
    }


@pytest.mark.parametrize("role,sender_type", [("user", "human"), ("assistant", "assistant"), ("system", "system")])
def test_sender_type_follows_the_role(role, sender_type) -> None:
    parser = ClaudeCodeTranscriptParser(dataset_id=DATASET)
    normalized = parser.parse(RawRecord(record_id="x", payload=record(role=role)))
    assert normalized.payload["sender_type"] == sender_type


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"role": "tool"}, "role"),
        ({"id": "chatgpt:abc"}, "id"),
        ({"content": "   "}, "content"),
        ({"content": ""}, "content"),
        ({"created_at": 1_760_000_000}, "created_at"),
        ({"_metadata": {"tool": "codex"}}, "_metadata.tool"),
    ],
)
def test_parsers_refuse_records_off_the_contract(overrides, fragment) -> None:
    parser = ClaudeCodeTranscriptParser(dataset_id=DATASET)
    result = parser.validate(RawRecord(record_id="x", payload=record(**overrides)))
    assert result.is_valid is False
    assert any(fragment in error for error in result.errors), result.errors


def test_a_codex_record_is_refused_by_the_claude_code_parser_and_vice_versa() -> None:
    assert ClaudeCodeTranscriptParser(dataset_id=DATASET).validate(
        RawRecord(record_id="x", payload=record("codex"))
    ).is_valid is False
    assert CodexTranscriptParser(dataset_id=DATASET).validate(
        RawRecord(record_id="x", payload=record("claude_code"))
    ).is_valid is False


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


def test_mapper_is_registered_when_the_parser_package_is_imported() -> None:
    assert MAPPER_ID == "ai_coding_tool"
    assert isinstance(get_mapper("ai_coding_tool"), CodingToolToAIChatMapper)


def test_mapper_mints_tool_prefixed_conversation_ids_from_the_record_metadata() -> None:
    mapper = CodingToolToAIChatMapper()
    staging = ClaudeCodeTranscriptParser(dataset_id=DATASET).parse(RawRecord(record_id="x", payload=record())).payload
    assert mapper.extract_conversation_id(staging) == "claude_code:sess-1"
    codex = CodexTranscriptParser(dataset_id=DATASET).parse(RawRecord(record_id="x", payload=record("codex"))).payload
    assert mapper.extract_conversation_id(codex) == "codex:sess-1"
    # No _metadata: the tool comes from the message id prefix, never "chatgpt".
    assert mapper.extract_conversation_id({"message_id": "cursor:abc", "thread_id": "c-9"}) == "cursor:c-9"


def test_mapper_merges_metadata_without_cwd_or_paths_and_keeps_the_title() -> None:
    mapper = CodingToolToAIChatMapper()
    payload = record()
    payload["_metadata"].update({
        "cwd": "/Users/fake/dev/widget",
        "project_path": "/Users/fake/dev/widget",
        "gitBranch": "main",
        "transcript_path": "/Users/fake/.claude/projects/x/y.jsonl",
        "hostname": "fakebox.local",
        "model_slug": "claude-fable-5-1",
    })
    staging = ClaudeCodeTranscriptParser(dataset_id=DATASET).parse(RawRecord(record_id="x", payload=payload)).payload
    staging["source_id"] = "claude_code_sessions"
    (message,) = mapper.map_to_canonical(staging, "ai_coding_tool")
    assert message.message_id == "claude_code:0000000a-0000-4000-8000-00000000000a"
    assert message.conversation_id == "claude_code:sess-1"
    assert message.sender_type == "human"
    assert message.source_id == "claude_code_sessions"
    assert message.ts == "2026-10-01T09:00:00.250Z"
    assert message.metadata_json == {
        "thread_id": "sess-1",
        "original_source": "ai_coding_tool",
        "tool": "claude_code",
        "session_id": "sess-1",
        "turn_index": 0,
        "conversation_title": "Uploader retry",
        "origin": "keyboard",
        "share_excluded": False,
        "timestamp_source": "record",
        "model_slug": "claude-fable-5-1",
    }
    blob = json.dumps(message.metadata_json)
    assert "/Users" not in blob and "fakebox" not in blob and "main" not in blob
    assert {"cwd", "project_path", "gitBranch", "transcript_path", "hostname"} <= EXCLUDED_METADATA_KEYS


@pytest.fixture()
def migrated_conn(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "coding_tool.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    yield conn


def test_canonical_pipeline_writes_coding_tool_rows_with_the_title_on_the_conversation(migrated_conn) -> None:
    from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
    from topos.sources.definitions import DataSourceDefinition

    source_def = DataSourceDefinition(**apply_bundled_canonical_defaults({
        "source_id": "claude_code_sessions",
        "display_name": "Claude Code sessions",
        "source_type": "local_sync",
        "delivery": "local_sync",
        "posture": "mixed",
        "schema_id": "claude_code.transcript.v1",
        "parser_id": "claude_code.transcript.v1",
        "canonical_group_id": "ai_messages",
        "default_scope_id": "ai_coding_sessions",
        "allowed_scope_ids": ["ai_coding_sessions:read"],
    }))
    assert source_def.canonical_mapper_id == "ai_coding_tool"
    parser = ClaudeCodeTranscriptParser(dataset_id=DATASET)
    reply = record(role="assistant", id="claude_code:0000000b-0000-4000-8000-00000000000b",
                   content="I'll add a retry with backoff.", created_at="2026-10-01T09:00:00.500Z")
    reply["_metadata"].update({"turn_index": 1, "origin": "assistant", "share_excluded": True})
    normalized = [parser.parse(RawRecord(record_id="x", payload=p)) for p in (record(), reply)]
    result = canonicalize_normalized_batch(
        migrated_conn, source_def, normalized, dataset_id=DATASET, sync_batch_id="batch-cc-1",
    )
    assert result.errors == []
    assert result.messages_created == 2
    rows = migrated_conn.execute(
        "SELECT message_id, conversation_id, sender_type, content, metadata_json FROM ai_chat_messages ORDER BY event_at"
    ).fetchall()
    assert [(r["message_id"], r["conversation_id"], r["sender_type"]) for r in rows] == [
        ("claude_code:0000000a-0000-4000-8000-00000000000a", "claude_code:sess-1", "human"),
        ("claude_code:0000000b-0000-4000-8000-00000000000b", "claude_code:sess-1", "assistant"),
    ]
    assert json.loads(rows[1]["metadata_json"])["share_excluded"] is True
    conv = migrated_conn.execute(
        "SELECT conversation_id, title FROM ai_chat_conversations"
    ).fetchall()
    assert [(c["conversation_id"], c["title"]) for c in conv] == [("claude_code:sess-1", "Uploader retry")]
