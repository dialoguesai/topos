"""Prov follow-up: authored goals must survive work paraphrases without 'goal'.

Router already sends these to work_context:read. Retrieval/planner gates must
keep + floor + first-person-shape the owner's user_goals — otherwise dense
"working on / toward / projects" asks drown under vector/recent lanes.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.planner import first_person_flags
from topos.query.retrieval import (
    _EXTRA_SURFACE_TERMS,
    _load_user_goal_summaries,
    _query_tokens,
    DefaultSignalRetrievalAdapter,
)
from topos.query.types import RetrievalRequest
from topos.sources.registry import CHATGPT_FILE, CHATGPT_UI, get_sources_by_scope
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations


@pytest.mark.parametrize(
    "query",
    [
        "What have I been working on lately?",
        "What am I working toward?",
        "What projects am I focused on?",
        "Work goals and projects",
    ],
)
def test_goal_intent_lexicon_covers_work_paraphrases(query: str) -> None:
    lower = query.lower()
    assert any(term in lower for term in _EXTRA_SURFACE_TERMS), query


@pytest.mark.parametrize(
    "query",
    [
        "What have I been working on lately?",
        "What am I working toward?",
        "What projects am I focused on?",
        "What are my work goals?",
    ],
)
def test_first_person_flags_cover_work_paraphrases(query: str) -> None:
    intent, _belief, _interaction = first_person_flags(query)
    assert intent is True, query


def test_toward_is_not_a_rare_content_token() -> None:
    tokens = _query_tokens("What am I working toward?")
    assert "toward" not in tokens
    assert "towards" not in tokens


def test_chatgpt_sources_allowed_on_work_context() -> None:
    assert "work_context:read" in CHATGPT_FILE.allowed_scope_ids
    assert "work_context:read" in CHATGPT_UI.allowed_scope_ids
    work_sources = set(get_sources_by_scope("work_context:read"))
    assert CHATGPT_FILE.source_id in work_sources
    assert CHATGPT_UI.source_id in work_sources


@pytest.fixture
def goals_conn(tmp_path):
    db_path = tmp_path / "authored_goals.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    c.execute(
        """
        INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, payload_json)
        VALUES
          ('g_ship', 'm1', 'chatgpt_file_ingestion', 'ship the provenance role gates', 'ollama', 'ollama', '{}'),
          ('g_dense', 'm2', 'chatgpt_file_ingestion', 'raise dense work-context recall', 'ollama', 'ollama', '{}')
        """
    )
    c.commit()
    yield c
    c.close()


@pytest.mark.parametrize(
    "query",
    [
        "What have I been working on lately?",
        "What am I working toward?",
        "What projects am I focused on?",
    ],
)
def test_load_user_goals_keeps_items_without_token_overlap(goals_conn, query: str) -> None:
    items = _load_user_goal_summaries(query, conn=goals_conn)
    assert {i["goal_id"] for i in items} >= {"g_ship", "g_dense"}


@pytest.mark.parametrize(
    "query",
    [
        "What have I been working on lately?",
        "What am I working toward?",
        "What projects am I focused on?",
    ],
)
def test_work_context_retrieve_surfaces_authored_goals(goals_conn, monkeypatch, query: str) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: goals_conn)
    adapters = AdapterFactory.create("local_database", conn=goals_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    # Empty install set: goals must still load via default_source_ids.
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text=query,
            installed_source_ids=[],
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    goal_summaries = [s for s in summaries if s.get("retrieval_source") == "user_goal"]
    assert len(goal_summaries) >= 2, [s.get("retrieval_source") for s in summaries]
