"""S6 — node-side pack attribution on engine LLM usage observations."""

from __future__ import annotations

import sqlite3

from topos.config.model_packs import (
    apply_sync_payload,
    attribution_for_engine_call,
    resolve_for_function,
    resolve_model,
    SOURCE_PACK,
)
from topos.engine.usage_observation import emit_engine_llm_usage_observation


def _memory_conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_every_function_role_resolves_through_node_resolver():
    pack = {
        "pack_id": "pack_test",
        "roles": {
            "primary": {"provider": "ollama", "model": "primary:latest"},
            "reasoning": {"provider": "ollama", "model": "reason:latest"},
            "tool": {"provider": "ollama", "model": "tool:latest"},
            "classify": {"provider": "ollama", "model": "classify:latest"},
        },
    }
    for key in (
        "facts",
        "conversation_context",
        "signal_extraction",
        "sanitization",
        "routine_orchestration",
        "routine_synthesis",
        "home_chat",
    ):
        resolved = resolve_for_function(key, pack=pack)
        assert resolved.source == SOURCE_PACK, key
        assert resolved.pack_id == "pack_test"


def test_attribution_for_engine_call_reads_active_pack():
    conn = _memory_conn()
    apply_sync_payload(
        conn,
        {
            "revision": 1,
            "active": "pack_active",
            "packs": [
                {
                    "pack_id": "pack_active",
                    "roles": {
                        "classify": {"provider": "ollama", "model": "llama3.2:latest"},
                    },
                }
            ],
        },
    )
    pack_id, role = attribution_for_engine_call(conn, subtype="fact_llm_extract")
    assert pack_id == "pack_active"
    assert role == "classify"


def test_emit_engine_llm_usage_observation_stamps_pack_and_role(monkeypatch):
    local_writes = []

    monkeypatch.setattr(
        "topos.engine.usage_observation._record_local_usage_best_effort",
        lambda **kwargs: local_writes.append(kwargs),
    )
    monkeypatch.setattr(
        "topos.config.model_packs.attribution_for_engine_call",
        lambda *_a, **_k: ("pack_active", "classify"),
    )

    import topos.core.state as engine_state

    monkeypatch.setattr(engine_state, "control_plane_client", None)
    monkeypatch.setattr(engine_state, "get_db_connection", lambda: object(), raising=False)

    envelope = emit_engine_llm_usage_observation(
        task_id="t1",
        task_type="enrichment",
        subtype="fact_llm_extract",
        provider="ollama",
        model="llama3.2:latest",
        usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        origin="ingestion_pipeline",
    )
    assert envelope is not None
    assert envelope["metadata"]["pack_id"] == "pack_active"
    assert envelope["metadata"]["role"] == "classify"
    assert local_writes[0]["metadata"]["pack_id"] == "pack_active"
    assert local_writes[0]["metadata"]["role"] == "classify"


def test_resolve_model_uninstalled_local_never_promotes_to_cloud():
    pack = {
        "pack_id": "p",
        "roles": {
            "tool": {"provider": "ollama", "model": "missing:latest"},
            "primary": {"provider": "openai", "model": "gpt-5"},
        },
    }
    resolved = resolve_model(
        role="tool",
        pack=pack,
        engine_default={"provider": "ollama", "model": "llama3.2:latest"},
        installed_local_models=["llama3.2:latest"],
    )
    assert resolved.source == "engine_default"
    assert resolved.provider == "ollama"
    assert resolved.model == "llama3.2:latest"
