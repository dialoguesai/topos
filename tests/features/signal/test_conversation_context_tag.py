"""Conversation context tag (work | personal): migration, API, and the
relationships-tier consumer."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from topos.features.signal.extraction.artifact_router import route_canonical_batch
from topos.features.signal.signal_object_store import SignalObjectStore
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations.conversation_context_v1 import (
    apply_conversation_context_v1_up,
)
from topos.storage.db.migrations.extraction_artifacts import apply_extraction_artifacts_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _conn() -> sqlite3.Connection:
    # check_same_thread=False: TestClient serves requests off-thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ensure_all_tables(conn)
    apply_conversation_context_v1_up(conn)
    apply_signal_objects_up(conn)
    apply_extraction_artifacts_up(conn)
    return conn


def _seed_conversation(conn: sqlite3.Connection, conversation_id: str, tag: str | None) -> None:
    conn.execute(
        "INSERT INTO conversations (conversation_id, dataset_id, source_id, context_tag) "
        "VALUES (?, 'ds-1', 'slack_activity', ?)",
        (conversation_id, tag),
    )


def _message(conversation_id: str, message_id: str) -> dict:
    return {
        "canonical_table": "conversation_messages",
        "message_id": message_id,
        "conversation_id": conversation_id,
        "sender_name": "Rook Halloway",
        "is_from_self": False,
        "content": "sync on the roadmap",
    }


def test_migration_idempotent_and_indexed() -> None:
    conn = _conn()
    apply_conversation_context_v1_up(conn)  # re-run must be a no-op
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    assert "context_tag" in cols


def test_work_context_keeps_professional_tier() -> None:
    conn = _conn()
    _seed_conversation(conn, "conv-work", "work")
    route_canonical_batch(conn, [_message("conv-work", "m-1")])
    edges = SignalObjectStore(conn).list_objects("relationships", object_type="RelationshipEdge")
    assert edges and edges[0]["payload"]["tier"] == "professional"


def test_personal_context_flips_tier() -> None:
    conn = _conn()
    _seed_conversation(conn, "conv-home", "personal")
    route_canonical_batch(conn, [_message("conv-home", "m-2")])
    edges = SignalObjectStore(conn).list_objects("relationships", object_type="RelationshipEdge")
    assert edges and edges[0]["payload"]["tier"] == "personal"


def test_unset_context_keeps_default() -> None:
    conn = _conn()
    _seed_conversation(conn, "conv-plain", None)
    route_canonical_batch(conn, [_message("conv-plain", "m-3")])
    edges = SignalObjectStore(conn).list_objects("relationships", object_type="RelationshipEdge")
    assert edges and edges[0]["payload"]["tier"] == "professional"


def test_context_api_set_get_and_validation(monkeypatch) -> None:
    conn = _conn()
    _seed_conversation(conn, "conv-api", None)
    conn.commit()

    from topos import app as app_module
    from topos.api import conversation_context as ctx_module
    from topos.config.settings import settings

    monkeypatch.setattr(ctx_module, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "topos_key", "test-key")
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer test-key"}

    resp = client.put(
        "/v1/conversations/conv-api/context",
        json={"dataset_id": "ds-1", "context_tag": "work"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["context_tag"] == "work"

    resp = client.get(
        "/v1/conversations/conv-api/context",
        params={"dataset_id": "ds-1"},
        headers=headers,
    )
    assert resp.json()["context_tag"] == "work"

    # Clear back to unset.
    resp = client.put(
        "/v1/conversations/conv-api/context",
        json={"dataset_id": "ds-1", "context_tag": None},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["context_tag"] is None

    # Invalid values and unknown conversations are refused.
    assert client.put(
        "/v1/conversations/conv-api/context",
        json={"dataset_id": "ds-1", "context_tag": "secret"},
        headers=headers,
    ).status_code == 422
    assert client.put(
        "/v1/conversations/nope/context",
        json={"dataset_id": "ds-1", "context_tag": "work"},
        headers=headers,
    ).status_code == 404
    assert client.put(
        "/v1/conversations/conv-api/context",
        json={"dataset_id": "ds-1", "context_tag": "work"},
    ).status_code in (401, 403)


def _seed_messages(conn: sqlite3.Connection, conversation_id: str, texts: list[str]) -> None:
    for i, text in enumerate(texts):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(message_id, conversation_id, dataset_id, sender_type, sender_id, "
            "content, event_at, source_id, is_from_self) "
            "VALUES (?, ?, 'ds-1', 'user', 'peer', ?, ?, 'slack_activity', 0)",
            (f"{conversation_id}-m{i}", conversation_id, text, f"2026-07-0{i+1}T10:00:00+00:00"),
        )


def test_auto_classifier_tags_untagged_only() -> None:
    from topos.features.signal.conversation_context import classify_untagged_conversations

    conn = _conn()
    _seed_conversation(conn, "conv-auto", None)
    _seed_conversation(conn, "conv-owner", None)
    conn.execute(
        "UPDATE conversations SET context_tag=NULL, context_tag_source='owner' "
        "WHERE conversation_id='conv-owner'"
    )
    _seed_messages(conn, "conv-auto", ["quarterly roadmap sync", "ship the release"])
    _seed_messages(conn, "conv-owner", ["dinner on friday?"])
    conn.commit()

    result = classify_untagged_conversations(conn, classify=lambda excerpts: "work")
    assert result == {"examined": 1, "tagged": 1}

    row = conn.execute(
        "SELECT context_tag, context_tag_source FROM conversations "
        "WHERE conversation_id='conv-auto'"
    ).fetchone()
    assert row[0] == "work" and str(row[1]).startswith("auto:")
    # Owner-cleared conversation untouched.
    row = conn.execute(
        "SELECT context_tag, context_tag_source FROM conversations "
        "WHERE conversation_id='conv-owner'"
    ).fetchone()
    assert row[0] is None and row[1] == "owner"


def test_auto_classifier_unclear_leaves_untagged() -> None:
    from topos.features.signal.conversation_context import classify_untagged_conversations

    conn = _conn()
    _seed_conversation(conn, "conv-vague", None)
    _seed_messages(conn, "conv-vague", ["hmm", "ok"])
    conn.commit()
    result = classify_untagged_conversations(conn, classify=lambda excerpts: None)
    assert result == {"examined": 1, "tagged": 0}
    row = conn.execute(
        "SELECT context_tag, context_tag_source FROM conversations "
        "WHERE conversation_id='conv-vague'"
    ).fetchone()
    assert row[0] is None and row[1] is None


def test_auto_classifier_no_model_is_noop(monkeypatch) -> None:
    from topos.features.signal import conversation_context as cc

    conn = _conn()
    _seed_conversation(conn, "conv-nomodel", None)
    _seed_messages(conn, "conv-nomodel", ["hello there"])
    conn.commit()
    import topos.config.conversation_context_llm as cfg

    monkeypatch.setattr(cfg, "resolve_context_llm_model", lambda settings, conn=None: "")
    assert cc.classify_untagged_conversations(conn) == {"examined": 0, "tagged": 0}


def test_parse_label_strictness() -> None:
    from topos.features.signal.conversation_context import _parse_label

    assert _parse_label("work") == "work"
    assert _parse_label("Personal") == "personal"
    assert _parse_label("it is work personal mix") is None
    assert _parse_label("unclear") is None
    assert _parse_label("") is None


def test_context_llm_config_api(monkeypatch) -> None:
    conn = _conn()
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    from topos import app as app_module
    from topos.api import conversation_context as ctx_module
    from topos.config.settings import settings

    monkeypatch.setattr(ctx_module, "get_db_connection", lambda: conn)
    import topos.core.state as state_module

    monkeypatch.setattr(
        state_module,
        "set_engine_config_value",
        lambda c, key, value: (
            c.execute(
                "INSERT INTO engine_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            ),
            c.commit(),
        ),
    )
    monkeypatch.setattr(settings, "topos_key", "test-key")
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer test-key"}

    resp = client.put(
        "/v1/conversation-context-llm-config",
        json={"model": "qwen3.5:9b-mlx"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["override"] == "qwen3.5:9b-mlx"
    assert resp.json()["source"] == "device_override"

    resp = client.get("/v1/conversation-context-llm-config", headers=headers)
    assert resp.json()["model"] == "qwen3.5:9b-mlx"

    # Garbage model names are refused.
    assert client.put(
        "/v1/conversation-context-llm-config",
        json={"model": "bad name with spaces"},
        headers=headers,
    ).status_code == 400


def _ws_call(msg_type: str, payload: dict) -> dict:
    import asyncio

    from topos.core.handlers import HANDLERS

    return asyncio.run(HANDLERS[msg_type]({"id": "req-1", "payload": payload}))


def test_context_ws_handlers_roundtrip(monkeypatch) -> None:
    """WS mirror of the HTTP context endpoints (CP proxy path)."""
    conn = _conn()
    _seed_conversation(conn, "conv-ws", None)
    conn.commit()

    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    put = _ws_call(
        "put_conversation_context",
        {"conversation_id": "conv-ws", "dataset_id": "ds-1", "context_tag": "work"},
    )
    assert put["status"] == "ok", put
    assert put["payload"]["context_tag"] == "work"
    assert put["payload"]["context_tag_source"] == "owner"

    got = _ws_call(
        "get_conversation_context", {"conversation_id": "conv-ws", "dataset_id": "ds-1"}
    )
    assert got["status"] == "ok"
    assert got["payload"]["context_tag"] == "work"

    cleared = _ws_call(
        "put_conversation_context",
        {"conversation_id": "conv-ws", "dataset_id": "ds-1", "context_tag": None},
    )
    assert cleared["payload"]["context_tag"] is None
    assert cleared["payload"]["context_tag_source"] == "owner"

    bad = _ws_call(
        "put_conversation_context",
        {"conversation_id": "conv-ws", "dataset_id": "ds-1", "context_tag": "secret"},
    )
    assert bad["status"] == "error" and "context_tag" in bad["error"]

    unknown = _ws_call(
        "get_conversation_context", {"conversation_id": "nope", "dataset_id": "ds-1"}
    )
    assert unknown["status"] == "error" and unknown["error"] == "Unknown conversation"

    missing_args = _ws_call("get_conversation_context", {"conversation_id": "conv-ws"})
    assert missing_args["status"] == "error" and "dataset_id" in missing_args["error"]


def test_context_llm_config_ws_handlers(monkeypatch) -> None:
    """WS mirror of the classifier model-config endpoints (CP proxy path)."""
    conn = _conn()
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    import topos.core.handlers as hub
    import topos.core.handlers.config as handlers_config

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    monkeypatch.setattr(
        handlers_config,
        "set_engine_config_value",
        lambda c, key, value: (
            c.execute(
                "INSERT INTO engine_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            ),
            c.commit(),
        ),
    )

    put = _ws_call("put_conversation_context_llm_config", {"model": "qwen3.5:9b-mlx"})
    assert put["status"] == "ok", put
    assert put["payload"]["override"] == "qwen3.5:9b-mlx"
    assert put["payload"]["source"] == "device_override"

    got = _ws_call("get_conversation_context_llm_config", {})
    assert got["status"] == "ok"
    assert got["payload"]["model"] == "qwen3.5:9b-mlx"

    cleared = _ws_call("put_conversation_context_llm_config", {"model": ""})
    assert cleared["status"] == "ok"
    assert cleared["payload"]["override"] is None

    bad = _ws_call("put_conversation_context_llm_config", {"model": "bad name with spaces"})
    assert bad["status"] == "error" and "model" in bad["error"]
