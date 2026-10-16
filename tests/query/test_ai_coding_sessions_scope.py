"""The ``ai_coding_sessions:read`` scope and the three AI-coding-tool sources (CONTRACTS.md §2.5).

Nothing new is shared (SPRINTS rule 4): the three sources sit in their own scope and are absent
from ``ai_conversations:read`` and ``work_context:read`` in every copy this checkout can see; a
grantee asking over ``ai_conversations`` gets no coding row, the owner asking over
``ai_coding_sessions`` gets them in raw mode with no embeddings and no model; a bare
``ai_coding_sessions`` snaps to the new scope, not to ``ai_conversations:read``. Invented rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

NEW_SCOPE = "ai_coding_sessions:read"
THREE = ("claude_code_sessions", "codex_sessions", "cursor_sessions")
NEEDLE = "quillfeather"
REPO = Path(__file__).resolve().parents[2]
NODE_REGISTRY = REPO / "topos" / "query" / "scope_registry.json"


def _node_scopes():
    with NODE_REGISTRY.open(encoding="utf-8") as fh:
        return {s["scope_id"]: s for s in json.load(fh)["scopes"]}


# --- the registry entry --------------------------------------------------------


def test_the_scope_entry_is_exactly_the_contract() -> None:
    entry = _node_scopes().get(NEW_SCOPE)
    assert entry is not None, f"{NEW_SCOPE} missing from {NODE_REGISTRY}"
    assert entry["raw_tables"] == ["ai_chat_messages"]
    assert entry["signal_objects"] == ["message_topics", "user_goals"]
    assert entry["summary_objects"] == ["memory_dimension_summary"]
    assert entry["inference_objects"] == ["goal_inference"]
    assert entry["default_mode_ceiling"] == "inference"
    assert entry["default_source_ids"] == list(THREE)
    assert entry["primary_dimensions"] == ["Work", "Memory"]
    assert entry["implementation_status"] == "live"
    assert entry["description"].startswith("AI coding sessions (Claude Code, Codex, Cursor)")
    assert len(entry["example_questions"]) == 2


def test_the_three_sources_are_absent_from_ai_conversations_and_work_context_in_every_copy() -> None:
    """Fails if any copy adds them: node registry (always), and the CP, FE and wiki copies when present."""
    scopes = _node_scopes()
    for scope_id in ("ai_conversations:read", "work_context:read"):
        present = set(scopes[scope_id]["default_source_ids"]) & set(THREE)
        assert not present, f"node {scope_id} lists {sorted(present)}"

    checked = ["node"]
    root = Path(os.environ.get("TOPOS_WORKSPACE_ROOT") or REPO.parents[2])
    cp_copy = root / "control_plane" / "uma" / "scope_registry.json"
    if cp_copy.exists():
        with cp_copy.open(encoding="utf-8") as fh:
            cp_scopes = {s["scope_id"]: s for s in json.load(fh)["scopes"]}
        for scope_id in ("ai_conversations:read", "work_context:read"):
            if scope_id in cp_scopes:
                present = set(cp_scopes[scope_id].get("default_source_ids") or []) & set(THREE)
                assert not present, f"CP {scope_id} lists {sorted(present)}"
        checked.append("cp")
    for rel in (
        "topos-react-app/next/features/permissions/scopeCatalog.ts",
        "topos-react-app/next/lib/mcp/scopeQueryRouter.ts",
        "topos-react-app/next/features/home/granteeQueryPlan.ts",
        "topos-react-app/next/features/signal/utils/graphSourceFilter.ts",
        "dialogues-data-wiki/scope_registry_mvp.json",
    ):
        copy = root / rel
        if not copy.exists():
            continue
        text = copy.read_text(encoding="utf-8")
        for scope_id in ("ai_conversations:read", "work_context:read"):
            block_start = text.find(scope_id)
            if block_start < 0:
                continue
            # The copy's block for this scope ends at the next scope id or the end of the file.
            next_scope = text.find(":read", block_start + len(scope_id))
            block = text[block_start: next_scope if next_scope > 0 else len(text)]
            present = [s for s in THREE if s in block]
            assert not present, f"{rel} {scope_id} block names {present}"
        checked.append(rel)
    assert "node" in checked


def test_a_bare_ai_coding_sessions_snaps_to_the_new_scope_not_ai_conversations() -> None:
    from topos.query.scope_registry_loader import suggest_scope_id

    assert suggest_scope_id("ai_coding_sessions") == NEW_SCOPE
    assert suggest_scope_id("ai_coding_sessions", "what did I build with claude code this week") == NEW_SCOPE
    assert suggest_scope_id("coding_sessions", "what did codex and I build") == NEW_SCOPE


def test_the_manifest_source_set_is_exactly_the_three() -> None:
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.sources.registry import get_sources_by_scope

    manifest = resolve_scope_manifest(NEW_SCOPE)
    assert manifest.default_source_ids == list(THREE)
    assert manifest.canonical_tables == ["ai_chat_messages"]
    assert set(get_sources_by_scope(NEW_SCOPE)) == set(THREE)
    for other in ("ai_conversations:read", "work_context:read"):
        assert not set(get_sources_by_scope(other)) & set(THREE), other
        assert not set(resolve_scope_manifest(other).default_source_ids) & set(THREE), other


# --- the source definitions ----------------------------------------------------


@pytest.mark.parametrize("source_id,schema_id,display", [
    ("claude_code_sessions", "claude_code.transcript.v1", "Claude Code"),
    ("codex_sessions", "codex.transcript.v1", "Codex"),
    ("cursor_sessions", "cursor.transcript.v1", "Cursor"),
])
def test_each_source_definition_follows_the_contract(source_id, schema_id, display) -> None:
    from topos.sources.registry import BUNDLED_REGISTRY, REGISTRY

    defn = REGISTRY[source_id]
    # A runtime install elsewhere in the suite may have replaced the REGISTRY entry with a rebuilt copy.
    assert BUNDLED_REGISTRY[source_id].to_dict() == defn.to_dict()
    assert defn.source_type == "local_sync" and defn.delivery == "local_sync"
    assert defn.posture == "mixed"
    assert defn.schema_id == schema_id and defn.parser_id == schema_id
    assert defn.canonical_group_id == "ai_messages" and defn.canonical_mapper_id == "ai_coding_tool"
    assert defn.canonical_enrichment_jobs == ["entities", "topics", "embeddings"]
    assert defn.signal_derivation_jobs == ["topics", "goal_extraction"]
    assert defn.analytics_profile_id == "chatgpt_dev"
    assert defn.default_scope_id == "ai_coding_sessions"
    assert defn.allowed_scope_ids == [NEW_SCOPE]
    assert defn.enrichment_trigger == "automatic" and defn.ingestion_trigger == "automatic"
    assert defn.filter_tier_kind == "sensitivity" and set(defn.default_filter_tiers) == {"low", "medium", "high"}
    assert defn.field_transform_defaults == [
        {"table_id": "ai_chat_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "ai_chat_messages", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ]
    assert display in defn.display_name


def test_the_per_source_lists_name_the_three() -> None:
    from topos.analytics.profiles import get_profile
    from topos.features.complexity.projection import SOURCE_INTENT
    from topos.features.signal.topic_clustering import MVP_QUERY_SOURCE_IDS
    from topos.query.aggregate import AGGREGATE_REGISTRY
    from topos.storage.raw.raw_tables_manager import RawTablesManager

    for source_id in THREE:
        assert get_profile(source_id) is not None and get_profile(source_id)["profile_id"] == "chatgpt_dev"
        assert source_id in SOURCE_INTENT
        assert source_id in MVP_QUERY_SOURCE_IDS
    assert AGGREGATE_REGISTRY[NEW_SCOPE].table == "ai_chat_messages"
    manager = RawTablesManager(sqlite3.connect(":memory:"))
    assert manager.get_raw_table_name("claude_code_sessions") == "raw_chat_messages_claude_code"
    assert manager.get_raw_table_name("codex_sessions") == "raw_chat_messages_codex"
    assert manager.get_raw_table_name("cursor_sessions") == "raw_chat_messages_cursor"


# --- queries over invented rows --------------------------------------------------


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from tests.ingestion.ai_tool_fixtures import build_node

    c = build_node(tmp_path / "scope.db")
    c.execute(
        "INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at) VALUES"
        " ('chatgpt:t1', 'owner-a', 'chat', 'chatgpt_ui_conversation', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z'),"
        " ('claude_code:s1', 'owner-a', 'code', 'claude_code_sessions', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, content_disclosure,"
        " source_id, share_excluded) VALUES"
        " ('chatgpt:m1', 'chatgpt:t1', 'human', '2026-07-02T19:00:00Z', 'quillfeather chatgpt prompt', 'quillfeather chatgpt prompt', 'chatgpt_ui_conversation', 0),"
        " ('claude_code:c1', 'claude_code:s1', 'human', '2026-07-03T19:00:00Z', 'quillfeather codingrow prompt', 'quillfeather codingrow prompt', 'claude_code_sessions', 0),"
        " ('claude_code:c2', 'claude_code:s1', 'assistant', '2026-07-03T19:00:01Z', 'quillfeather taintedrow reply', 'quillfeather taintedrow reply', 'claude_code_sessions', 1)"
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(scope: str, *, grantee: bool, mode: str = "raw") -> dict:
    payload = {
        "scope_id": scope,
        "access_mode": mode,
        "intent": NEEDLE,
        "query": NEEDLE,
        "query_session_id": f"coding-scope-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            disclosure_tier="default_disclosure", disclosure_ceiling="default",
            owner_user_id="owner-a", owner_id="owner-a", requester_id="owner-a",
        )
        principal = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="outside-client", acting_user="owner-a")
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


@pytest.mark.asyncio
async def test_a_grantee_over_ai_conversations_gets_no_coding_row(conn) -> None:
    payload = await _ask("ai_conversations:read", grantee=True)
    wire = json.dumps(payload, default=str)
    assert "chatgpt prompt" in wire, "positive control: the chat row still reaches the grantee"
    assert "codingrow" not in wire and "taintedrow" not in wire and "claude_code:c" not in wire


@pytest.mark.asyncio
async def test_the_owner_over_ai_coding_sessions_gets_the_coding_rows_raw_with_no_model(conn, monkeypatch) -> None:
    """Raw mode: no embeddings exist and no model is reachable; the row still comes back."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:1")
    payload = await _ask(NEW_SCOPE, grantee=False)
    assert payload.get("disclosure_tier") == "owner_raw"
    wire = json.dumps(payload, default=str)
    assert "codingrow" in wire and "taintedrow" in wire, "the owner keeps every row, tainted included"
    assert "chatgpt prompt" not in wire


@pytest.mark.asyncio
async def test_a_grantee_over_ai_coding_sessions_never_sees_a_share_excluded_row(conn) -> None:
    payload = await _ask(NEW_SCOPE, grantee=True)
    wire = json.dumps(payload, default=str)
    assert "codingrow" in wire, "positive control: the plain coding row reaches a grantee of the new scope"
    assert "taintedrow" not in wire and "claude_code:c2" not in wire
