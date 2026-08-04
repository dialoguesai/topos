"""B11: D3-like messenger harness — messages:read must surface authored user_goals.

Enrichment wiring alone is insufficient: messages:read previously omitted
user_goals from signal_objects, so imessage goals could never fuse even after
extract. This locks the retrieve path with a seeded imessage goal pair.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter, _load_user_goal_summaries
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def messenger_goals_conn(tmp_path):
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    db_path = tmp_path / "messenger_goals.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    ensure_all_tables(c)
    CanonicalTablesManager(c)
    c.execute(
        """
        INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, payload_json)
        VALUES
          ('g_msg_ship', 'im-1', 'imessage', 'ship the messenger goal extract backfill', 'ollama', 'ollama', '{}'),
          ('g_msg_eval', 'im-2', 'imessage', 'cover D3-like messenger goal cases in evals', 'ollama', 'ollama', '{}'),
          ('g_chat', 'cg-1', 'chatgpt_file_ingestion', 'chatgpt-only goal must not leak into messages scope', 'ollama', 'ollama', '{}')
        """
    )
    c.commit()
    yield c
    c.close()


def test_load_user_goals_respects_messenger_source_filter(messenger_goals_conn) -> None:
    items = _load_user_goal_summaries(
        "What goals have I mentioned in my messages?",
        source_ids=["imessage", "signal", "demo_messenger_file"],
        conn=messenger_goals_conn,
    )
    ids = {i["goal_id"] for i in items}
    assert ids >= {"g_msg_ship", "g_msg_eval"}
    assert "g_chat" not in ids


@pytest.mark.parametrize(
    "query",
    [
        "What goals have I mentioned in my messages?",
        "What am I working toward?",
        "What projects am I focused on?",
    ],
)
def test_messages_retrieve_surfaces_imessage_goals(
    messenger_goals_conn, monkeypatch, query: str
) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: messenger_goals_conn)
    adapters = AdapterFactory.create("local_database", conn=messenger_goals_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("messages:read")
    assert "user_goals" in (manifest.signal_objects or [])
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
    assert all(s.get("source_id") == "imessage" for s in goal_summaries)
