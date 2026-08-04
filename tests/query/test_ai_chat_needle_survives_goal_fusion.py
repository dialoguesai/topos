"""A3 C26: AI-chat known-item needles must survive goal/stat fusion crowding.

ai_conversations:read loads user_goals (extracted from the same message ids).
When the ask literally contains "goal", goal-intent + shared fusion keys let the
shorter goal_text replace the full chat row — the needle fragment only present
in the message never reaches the summary blob.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter, _fusion_item_key
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

NEEDLE = "coverage, and pursue edtech"
FULL_MESSAGE = (
    "My Q2 goals: ship goal extraction for the personal AI node, deepen UMA "
    f"scope {NEEDLE} pilots in Austin."
)
GOAL_TEXT = "Ship goal extraction for the personal AI node"
QUERY = "Find my AI conversations about goals extraction personal"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "c26_ai_chat_needle.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    c.execute(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (
            'msg-user-goals-1', 'conv-goals', 'human', 'chatgpt_file_ingestion',
            ?, '2026-06-01T12:00:00Z'
        )
        """,
        (FULL_MESSAGE,),
    )
    # Extracted goal shares record_id with the chat message (production shape).
    c.execute(
        """
        INSERT INTO user_goals (
            goal_id, record_id, source_id, goal_text, model, provider, payload_json
        ) VALUES (
            'g-extract', 'msg-user-goals-1', 'chatgpt_file_ingestion',
            ?, 'ollama', 'ollama', '{}'
        )
        """,
        (GOAL_TEXT,),
    )
    # Filler goals so goal-intent flooring alone cannot explain a needle hit.
    for i in range(8):
        c.execute(
            """
            INSERT INTO user_goals (
                goal_id, record_id, source_id, goal_text, model, provider, payload_json
            ) VALUES (?, ?, 'chatgpt_file_ingestion', ?, 'ollama', 'ollama', '{}')
            """,
            (f"g-fill-{i}", f"msg-fill-{i}", f"Filler personal goal number {i} extraction"),
        )
    c.commit()
    yield c
    c.close()


def test_user_goal_fusion_key_distinct_from_source_message() -> None:
    goal = {
        "record_id": "msg-user-goals-1",
        "goal_id": "g-extract",
        "retrieval_source": "user_goal",
        "summary_text": GOAL_TEXT,
    }
    message = {
        "record_id": "msg-user-goals-1",
        "retrieval_source": "canonical:ai_chat_messages",
        "summary_text": FULL_MESSAGE,
    }
    assert _fusion_item_key(goal) != _fusion_item_key(message)
    assert _fusion_item_key(goal).startswith("rec:msg-user-goals-1|goal:")


def test_ai_conversations_summary_surfaces_chat_needle_under_goal_fusion(
    conn, monkeypatch
) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("ai_conversations:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text=QUERY,
            disclosure_tier="owner_raw",
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    blob = " ".join(
        str(item.get("summary_text") or item.get("topic") or "") for item in summaries
    )
    assert NEEDLE in blob, [item.get("retrieval_source") for item in summaries]
    assert any(
        NEEDLE in str(item.get("summary_text") or "")
        and str(item.get("retrieval_source") or "") == "canonical:ai_chat_messages"
        for item in summaries
    )


def test_work_context_still_floors_authored_goals(conn, monkeypatch) -> None:
    """C26 floor must not remove the authored-goals work_context pin."""
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What have I been working on lately?",
            disclosure_tier="owner_raw",
            installed_source_ids=[],
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    goal_summaries = [s for s in summaries if s.get("retrieval_source") == "user_goal"]
    assert len(goal_summaries) >= 2, [s.get("retrieval_source") for s in summaries]
