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


# --- Time-scoped goal asks ("yesterday's goals") -----------------------------------------
#
# The planner resolves "yesterday" into plan.time_range; the goal lane joins
# user_goals.record_id -> timeline.event_at (the source message's event time —
# created_at is only ingest time) and prefers in-window goals, with an
# annotated fallback when nothing is in-window.

NOW = "2026-07-17T12:00:00+00:00"


@pytest.fixture
def dated_goals_conn(tmp_path):
    db_path = tmp_path / "dated_goals.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    for message_id, event_at, content in (
        ("m_yday", "2026-07-16T09:30:00+00:00", "ship the timeline join for goals"),
        ("m_old", "2026-06-01T09:30:00+00:00", "install Docker Desktop"),
    ):
        c.execute(
            """
            INSERT INTO ai_chat_messages (
                message_id, conversation_id, sender_type, source_id, content, event_at
            ) VALUES (?, 'conv1', 'user', 'chatgpt_ingestion', ?, ?)
            """,
            (message_id, content, event_at),
        )
        c.execute(
            "INSERT INTO timeline (event_at, record_id, source_id, canonical_table) VALUES (?, ?, 'chatgpt_ingestion', 'ai_chat_messages')",
            (event_at, message_id),
        )
    c.execute(
        """
        INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, payload_json)
        VALUES ('g_yday', 'm_yday', 'chatgpt_ingestion', 'ship the timeline join for goals', 'ollama', 'ollama', '{}')
        """
    )
    c.execute(
        """
        INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, payload_json)
        VALUES ('g_old', 'm_old', 'chatgpt_ingestion', 'install Docker Desktop', 'ollama', 'ollama', '{}')
        """
    )
    c.commit()
    yield c
    c.close()


def test_goal_items_carry_event_at_from_timeline(dated_goals_conn) -> None:
    from topos.query.retrieval import _load_user_goal_summaries

    items = _load_user_goal_summaries("my goals", conn=dated_goals_conn)
    by_id = {item["goal_id"]: item for item in items}
    assert by_id["g_yday"]["event_at"] == "2026-07-16T09:30:00+00:00"
    assert by_id["g_old"]["event_at"] == "2026-06-01T09:30:00+00:00"
    assert by_id["g_yday"]["created_at"]


def test_goal_lane_prefers_time_window(dated_goals_conn) -> None:
    from topos.query.retrieval import _load_user_goal_summaries

    items = _load_user_goal_summaries(
        "my goals",
        conn=dated_goals_conn,
        time_range=("2026-07-16T00:00:00+00:00", "2026-07-16T23:59:59+00:00"),
    )
    assert [item["goal_id"] for item in items] == ["g_yday"]
    assert items[0]["in_time_window"] is True


def test_goal_lane_window_fallback_is_annotated(dated_goals_conn) -> None:
    from topos.query.retrieval import _load_user_goal_summaries

    items = _load_user_goal_summaries(
        "my goals",
        conn=dated_goals_conn,
        time_range=("2026-07-10T00:00:00+00:00", "2026-07-10T23:59:59+00:00"),
    )
    # Nothing on 2026-07-10: soft fallback returns the dated goals but marks
    # every item out-of-window so synthesis cannot pass them off as in-range.
    assert len(items) == 2
    assert all(item["in_time_window"] is False for item in items)


def test_goal_lane_survives_missing_timeline_table(dated_goals_conn) -> None:
    from topos.query.retrieval import _load_user_goal_summaries

    dated_goals_conn.execute("DROP TABLE timeline")
    items = _load_user_goal_summaries("my goals", conn=dated_goals_conn)
    by_id = {item["goal_id"]: item for item in items}
    assert set(by_id) == {"g_yday", "g_old"}
    # Ingest-time fallback: still dated, just not event-anchored.
    assert by_id["g_yday"]["event_at"] == by_id["g_yday"]["created_at"]


def test_yesterdays_goals_end_to_end(dated_goals_conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: dated_goals_conn)
    adapters = AdapterFactory.create("local_database", conn=dated_goals_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What were my goals yesterday?",
            now=NOW,
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    goal_summaries = [s for s in summaries if s.get("retrieval_source") == "user_goal"]
    assert goal_summaries
    assert {s.get("goal_id") for s in goal_summaries} == {"g_yday"}
    assert goal_summaries[0].get("event_at") == "2026-07-16T09:30:00+00:00"
    assert goal_summaries[0].get("in_time_window") is True


def test_signal_fact_items_carry_created_at(dated_goals_conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=dated_goals_conn)
    adapters.signal.put_fact(
        {"dimension": "work", "source_id": "chatgpt_ingestion", "record_id": "m_yday", "summary_text": "worked on timeline join"}
    )
    page = adapters.signal.get_by_dimension("work")
    assert page.items
    assert all(item.get("created_at") for item in page.items)


# --- M1: canonical lane carries event_at + honors the plan window ------------------------


@pytest.fixture
def dated_canonical_conn(tmp_path):
    db_path = tmp_path / "dated_canonical.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    for message_id, event_at, content in (
        ("m_docker_yday", "2026-07-16T10:00:00+00:00", "debugged the docker compose stack"),
        ("m_docker_old", "2026-06-01T10:00:00+00:00", "first tried docker on the laptop"),
    ):
        c.execute(
            """
            INSERT INTO ai_chat_messages (
                message_id, conversation_id, sender_type, source_id, content, event_at
            ) VALUES (?, 'conv1', 'user', 'chatgpt_ingestion', ?, ?)
            """,
            (message_id, content, event_at),
        )
    c.commit()
    yield c
    c.close()


def test_canonical_items_carry_event_at(dated_canonical_conn, monkeypatch) -> None:
    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: dated_canonical_conn
    )
    adapters = AdapterFactory.create("local_database", conn=dated_canonical_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("ai_conversations:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What have I done with docker?",
            now=NOW,
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    canonical = [
        s for s in summaries
        if str(s.get("retrieval_source") or "").startswith("canonical:")
    ]
    assert canonical
    assert all(s.get("event_at") for s in canonical)


def test_canonical_lane_prefers_yesterday_window(dated_canonical_conn, monkeypatch) -> None:
    monkeypatch.setattr(
        "topos.core.state.get_db_connection", lambda: dated_canonical_conn
    )
    adapters = AdapterFactory.create("local_database", conn=dated_canonical_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("ai_conversations:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What did I do with docker yesterday?",
            now=NOW,
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    canonical = [
        s for s in summaries
        if str(s.get("retrieval_source") or "").startswith("canonical:")
    ]
    assert canonical
    assert {s.get("record_id") for s in canonical} == {"m_docker_yday"}
    assert all(s.get("in_time_window") is True for s in canonical)


# --- M2: vector lane hits carry event_at -------------------------------------------------


def test_semantic_hits_carry_event_at(monkeypatch) -> None:
    from topos.query import retrieval as retrieval_mod

    class _FakeService:
        def search_vectors(self, **kwargs):
            return {
                "items": [
                    {
                        "record_id": "m_vec",
                        "text_preview": "docker compose debugging",
                        "similarity": 0.91,
                        "source_id": "chatgpt_ingestion",
                        "signal_dimension": "work",
                        "event_at": "2026-07-16T10:00:00+00:00",
                    }
                ],
                "total": 1,
            }

    monkeypatch.setattr(
        "topos.features.signal.service.get_signal_service", lambda: _FakeService()
    )
    hits, error = retrieval_mod._semantic_hits("docker yesterday")
    assert error is None
    assert hits[0]["event_at"] == "2026-07-16T10:00:00+00:00"


# --- M4: parsed time window surfaces in the packet/public_result -------------------------


def test_time_window_surfaced_for_dated_query(dated_goals_conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: dated_goals_conn)
    adapters = AdapterFactory.create("local_database", conn=dated_goals_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What were my goals yesterday?",
            now=NOW,
        )
    )
    window = bundle.context_packet.get("time_window")
    assert window == {
        "source": "query_planner",
        "from": "2026-07-16T00:00:00+00:00",
        "to": "2026-07-16T23:59:59+00:00",
    }


def test_time_window_absent_for_untimed_query(dated_goals_conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: dated_goals_conn)
    adapters = AdapterFactory.create("local_database", conn=dated_goals_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("work_context:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What are my goals?",
            now=NOW,
        )
    )
    assert "time_window" not in bundle.context_packet


def test_game_layer_copies_time_window_into_summary_payload() -> None:
    from topos.query.game_layer import DefaultGameLayer

    result = DefaultGameLayer().apply(
        context_packet={
            "summaries": [{"topic": "t"}],
            "time_window": {"source": "query_planner", "from": "a", "to": "b"},
        },
        access_mode="summary",
        scope_id="work_context:read",
        query_text="what happened yesterday",
    )
    assert result.payload.get("time_window") == {
        "source": "query_planner", "from": "a", "to": "b",
    }
