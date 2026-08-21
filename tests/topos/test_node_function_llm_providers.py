"""Provider-aware node-function LLM configs (facts + conversation-context).

The device override can now name a hosted provider (platform / openai /
redpill) next to the model. Legacy writes ({"model": ...} with no provider)
must keep behaving exactly as before: stored provider "" resolves as ollama
through the historical fallback chains.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from topos.config.conversation_context_llm import (
    ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL,
    ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_PROVIDER,
    resolve_context_llm_request,
)
from topos.config.conversation_context_llm import (
    normalize_put_config as ctx_normalize_put_config,
)
from topos.config.facts_llm import (
    ENGINE_CONFIG_KEY_FACTS_LLM_MODEL,
    ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER,
    effective_config_for_api as facts_effective_config,
    normalize_put_config as facts_normalize_put_config,
    resolve_facts_llm_request,
)
from topos.engine.backends.redpill import DEFAULT_REDPILL_MODEL


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)", (key, value))


_SETTINGS = SimpleNamespace(
    facts_llm_model="",
    ollama_extraction_model="extraction:latest",
    ollama_query_model="query:latest",
    openai_model="gpt-4o-mini",
)


class TestNormalizePutConfig:
    def test_legacy_model_only_stores_empty_provider(self):
        assert facts_normalize_put_config({"model": "llama3.2:latest"}) == ("", "llama3.2:latest")
        assert ctx_normalize_put_config({"model": "llama3.2:latest"}) == ("", "llama3.2:latest")

    def test_explicit_ollama_folds_to_legacy_shape(self):
        assert facts_normalize_put_config({"provider": "ollama", "model": "m:1"}) == ("", "m:1")

    def test_empty_clears_both(self):
        assert facts_normalize_put_config(None) == ("", "")
        assert facts_normalize_put_config({"model": ""}) == ("", "")

    def test_hosted_provider_round_trips(self):
        assert facts_normalize_put_config(
            {"provider": "redpill", "model": DEFAULT_REDPILL_MODEL}
        ) == ("redpill", DEFAULT_REDPILL_MODEL)

    def test_platform_needs_no_model(self):
        assert facts_normalize_put_config({"provider": "platform"}) == ("platform", "")

    def test_hosted_provider_requires_model(self):
        with pytest.raises(ValueError):
            facts_normalize_put_config({"provider": "redpill"})
        with pytest.raises(ValueError):
            ctx_normalize_put_config({"provider": "openai"})

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError):
            facts_normalize_put_config({"provider": "grok", "model": "x"})


class TestResolveRequest:
    def test_no_override_is_ollama_chain(self):
        conn = _conn()
        assert resolve_facts_llm_request(_SETTINGS, conn) == ("ollama", "extraction:latest")
        assert resolve_context_llm_request(_SETTINGS, conn) == ("ollama", "extraction:latest")

    def test_legacy_model_override_stays_ollama(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, "pinned:latest")
        assert resolve_facts_llm_request(_SETTINGS, conn) == ("ollama", "pinned:latest")

    def test_hosted_override_resolves_hosted_model(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, "redpill")
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, DEFAULT_REDPILL_MODEL)
        assert resolve_facts_llm_request(_SETTINGS, conn) == ("redpill", DEFAULT_REDPILL_MODEL)

    def test_platform_override_defaults_model(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_PROVIDER, "platform")
        assert resolve_context_llm_request(_SETTINGS, conn) == ("platform", "gpt-4o-mini")

    def test_garbage_provider_value_falls_back_to_ollama(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, "grok")
        assert resolve_facts_llm_request(_SETTINGS, conn)[0] == "ollama"


class TestEffectiveConfigForApi:
    def test_provider_fields_present_and_sourced(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, "redpill")
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, DEFAULT_REDPILL_MODEL)
        data = facts_effective_config(_SETTINGS, conn)
        assert data["provider"] == "redpill"
        assert data["model"] == DEFAULT_REDPILL_MODEL
        assert data["device_override_provider"] == "redpill"
        assert data["source"] == "device_override"

    def test_provider_only_override_is_a_device_override(self):
        conn = _conn()
        _set(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER, "platform")
        data = facts_effective_config(_SETTINGS, conn)
        assert data["provider"] == "platform"
        assert data["source"] == "device_override"

    def test_no_override_reports_ollama(self):
        conn = _conn()
        data = facts_effective_config(_SETTINGS, conn)
        assert data["provider"] == "ollama"
        assert data["device_override_provider"] == ""
