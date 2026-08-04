"""D1.8 / B1 — unread surfaces: wire emotions OR assert absence for the rest.

Decision table (2026-08-03):
  message_emotions        → WIRE (role-filtered mood aggregate)
  timeline                → KEEP (already wires goals dating)
  messenger_social_edges  → KEEP analytics; absence from retrieval
  graph_edges/graph_nodes → GC deprecate; no retrieval furniture
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from topos.enrichment.derived_tables import (
    DerivedTablesManager,
    reset_ensured_tables_cache,
)
from topos.features.lifecycle.gc import DEPRECATED_TABLES, mark_deprecated_tables
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import (
    DefaultSignalRetrievalAdapter,
    _load_emotion_summary_items,
    _mood_emotion_intent,
)
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.canonical.conversations_tables import ensure_all_tables as ensure_conversation_tables
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.check("C-quality-unread-surfaces-d18")]


@pytest.fixture()
def emo_conn(tmp_path: Path):
    c = sqlite3.connect(str(tmp_path / "d18_emo.db"))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    ensure_conversation_tables(c)
    c.execute("DROP TABLE IF EXISTS message_emotions")
    c.commit()
    reset_ensured_tables_cache()
    DerivedTablesManager(conn=c)._ensure_tables()
    writer = DerivedTablesManager(conn=c)
    writer.write_enrichment_batch(
        [
            {
                "message_id": "m_joy_1",
                "source_id": "imessage",
                "emotion_label": "joy",
                "confidence": 0.91,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "authored",
            },
            {
                "message_id": "m_joy_2",
                "source_id": "imessage",
                "emotion_label": "joy",
                "confidence": 0.88,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "authored",
            },
            {
                "message_id": "m_sad_addr",
                "source_id": "imessage",
                "emotion_label": "sadness",
                "confidence": 0.8,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "addressed",
            },
            {
                "message_id": "m_anger_obs",
                "source_id": "imessage",
                "emotion_label": "anger",
                "confidence": 0.99,
                "model_name": "fake-emo",
                "all_emotions": [],
                "role": "observed",
            },
        ],
        "message_emotions",
    )
    yield c
    c.close()


def test_mood_intent_detects_emotion_asks() -> None:
    assert _mood_emotion_intent("How was my mood last week?")
    assert _mood_emotion_intent("What emotions show up in my texts?")
    assert not _mood_emotion_intent("Who do I message most?")


def test_emotion_loader_filters_observed(emo_conn) -> None:
    items = _load_emotion_summary_items(emo_conn)
    assert items
    blob = " ".join(str(i.get("summary_text") or "") for i in items).lower()
    assert "joy" in blob
    assert "sadness" in blob
    assert "anger" not in blob  # observed excluded
    assert all(i.get("retrieval_source") == "message_emotions" for i in items)


def test_messages_mood_ask_surfaces_emotions(emo_conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: emo_conn)
    adapters = AdapterFactory.create("local_database", conn=emo_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("messages:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="How was my mood lately?",
            disclosure_tier="owner_raw",
        )
    )
    summaries = (bundle.context_packet or {}).get("summaries") or []
    emo = [s for s in summaries if s.get("retrieval_source") == "message_emotions"]
    assert emo, f"expected message_emotions in summaries, got {[s.get('retrieval_source') for s in summaries]}"
    blob = " ".join(str(s.get("summary_text") or "") for s in emo).lower()
    assert "joy" in blob
    assert "anger" not in blob


def test_health_mood_ask_surfaces_emotions(emo_conn, monkeypatch) -> None:
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: emo_conn)
    adapters = AdapterFactory.create("local_database", conn=emo_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("health:read")
    assert "message_emotions" in (manifest.signal_objects or [])
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="How has my mood been?",
            disclosure_tier="owner_raw",
        )
    )
    summaries = (bundle.context_packet or {}).get("summaries") or []
    emo = [s for s in summaries if s.get("retrieval_source") == "message_emotions"]
    assert emo


def test_timeline_referenced_for_goals_dating() -> None:
    """BEFORE zombie claim was 'timeline unread'; AFTER: goals dating subquery."""
    from topos.query import retrieval as retrieval_mod

    src = inspect.getsource(retrieval_mod._load_user_goal_summaries)
    assert "FROM timeline" in src


def test_retrieval_absent_messenger_social_edges() -> None:
    from topos.query import retrieval as retrieval_mod

    src = inspect.getsource(retrieval_mod)
    assert "messenger_social_edges" not in src


def test_retrieval_absent_legacy_graph_furniture(emo_conn, monkeypatch) -> None:
    """graph_edges may exist; summary/inference packets must not attach them."""
    emo_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_nodes (
            node_id TEXT PRIMARY KEY, node_type TEXT, label TEXT,
            metadata_json TEXT, source_id TEXT
        )
        """
    )
    emo_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_edges (
            edge_id TEXT PRIMARY KEY, src_node_id TEXT, dst_node_id TEXT,
            edge_type TEXT, weight REAL, metadata_json TEXT, source_id TEXT
        )
        """
    )
    emo_conn.execute(
        "INSERT OR REPLACE INTO graph_nodes (node_id, metadata_json) "
        "VALUES ('n1', '{\"node_id\":\"n1\",\"label\":\"Zombie\"}')"
    )
    emo_conn.execute(
        "INSERT OR REPLACE INTO graph_edges (edge_id, src_node_id, dst_node_id, metadata_json) "
        "VALUES ('e1', 'n1', 'n1', '{\"edge_id\":\"e1\"}')"
    )
    emo_conn.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: emo_conn)
    adapters = AdapterFactory.create("local_database", conn=emo_conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    # messages:read ceiling is summary — enough to prove furniture is gone
    # (list_graph packaging removed from both summary and inference branches).
    from topos.query import retrieval as retrieval_mod

    assert "list_graph" not in inspect.getsource(retrieval_mod)
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"),
            access_mode="summary",
            query_text="Show my recent messages",
            disclosure_tier="owner_raw",
        )
    )
    packet = bundle.context_packet or {}
    assert "graph" not in packet or not (packet.get("graph") or {}).get("edges")
    assert "graph" not in (bundle.stores_touched or [])


def test_graph_tables_marked_deprecated() -> None:
    assert "graph_edges" in DEPRECATED_TABLES
    assert "graph_nodes" in DEPRECATED_TABLES
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE wiki_table_catalog (
            table_name TEXT PRIMARY KEY, authoritative_table TEXT,
            status TEXT, deprecation_note TEXT, updated_at TEXT
        )
        """
    )
    conn.execute("CREATE TABLE graph_edges (edge_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE graph_nodes (node_id TEXT PRIMARY KEY)")
    marked = mark_deprecated_tables(conn)
    assert marked >= 2
    for table in ("graph_edges", "graph_nodes"):
        row = conn.execute(
            "SELECT status, deprecation_note FROM wiki_table_catalog WHERE table_name=?",
            (table,),
        ).fetchone()
        assert row is not None
        assert row[0] == "deprecated"
        assert "entity" in (row[1] or "").lower()
    # Tables remain (GC marks, does not drop).
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='graph_edges'"
    ).fetchone()
