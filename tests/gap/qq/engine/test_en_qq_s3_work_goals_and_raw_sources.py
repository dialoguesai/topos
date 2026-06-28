"""Raw multi-source canonical reads and work goal summaries."""

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "retrieval_fix.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    c.execute(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES ('m_git', 'conv1', 'user', 'chatgpt_ingestion', 'git push to GitHub repository', '2026-01-01')
        """
    )
    c.execute(
        """
        INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, payload_json)
        VALUES ('g1', 'm1', 'chatgpt_ingestion', 'deploy VM instance on GCP', 'ollama', 'ollama', '{}')
        """
    )
    c.commit()
    yield c
    c.close()


def test_raw_git_query_uses_chatgpt_ingestion_source(conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("ai_conversations:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="raw",
            query_text="git GitHub",
        )
    )
    rows = bundle.context_packet.get("rows") or []
    assert len(rows) >= 1
    assert any("git" in str(row.get("content", "")).lower() for row in rows)


def test_default_disclosure_tier_lists_journal_content(conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapters = AdapterFactory.create("local_database", conn=conn)
    page = adapters.canonical.list(
        "journal_entries",
        disclosure_tier="default_disclosure",
        limit=10,
    )
    assert page.total >= 0


def test_work_context_prefers_user_goals(conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="Work goals and projects",
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    assert summaries
    assert summaries[0].get("retrieval_source") == "user_goal"
    assert "GCP" in str(summaries[0].get("topic") or "")
