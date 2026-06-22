"""Tests for signal extraction model config resolution."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.config.settings import Settings
from topos.config.signal_extraction import (
    ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
    effective_config_for_api,
    normalize_put_device_overrides,
    resolve_signal_extraction_model_request,
    resolve_signal_extraction_query_model,
)


@pytest.fixture()
def memory_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOPOS_KEY", "test-key-for-settings")
    monkeypatch.setenv("TOPOS_OLLAMA_QUERY_MODEL", "llama3.2:latest")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    return Settings()


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def test_resolve_signal_extraction_query_model_uses_env_default(memory_settings: Settings) -> None:
    conn = _memory_conn()
    assert resolve_signal_extraction_query_model(memory_settings, conn) == "llama3.2:latest"


def test_resolve_signal_extraction_query_model_device_override(memory_settings: Settings) -> None:
    conn = _memory_conn()
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (
            ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
            json.dumps({"version": 1, "provider": "ollama", "query_model": "mistral:latest"}),
        ),
    )
    conn.commit()
    assert resolve_signal_extraction_query_model(memory_settings, conn) == "mistral:latest"


def test_resolve_signal_extraction_model_request_platform(memory_settings: Settings) -> None:
    conn = _memory_conn()
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (
            ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
            json.dumps({"version": 1, "provider": "platform"}),
        ),
    )
    conn.commit()
    provider, model = resolve_signal_extraction_model_request(memory_settings, conn)
    assert provider == "openai"
    assert model == "gpt-4o-mini"


def test_resolve_signal_extraction_model_request_redpill(memory_settings: Settings) -> None:
    conn = _memory_conn()
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (
            ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
            json.dumps({"version": 1, "provider": "redpill", "query_model": "qwen/qwen3.6-27b"}),
        ),
    )
    conn.commit()
    provider, model = resolve_signal_extraction_model_request(memory_settings, conn)
    assert provider == "redpill"
    assert model == "qwen/qwen3.6-27b"


def test_effective_config_for_api_shape(memory_settings: Settings) -> None:
    conn = _memory_conn()
    payload = effective_config_for_api(memory_settings, conn)
    assert payload["defaults_from_settings"]["provider"] == "ollama"
    assert payload["defaults_from_settings"]["query_model"] == "llama3.2:latest"
    assert payload["effective"]["provider"] == "ollama"
    assert payload["effective"]["query_model"] == "llama3.2:latest"


def test_normalize_put_device_overrides_platform_without_model(memory_settings: Settings) -> None:
    raw = normalize_put_device_overrides({"device_overrides": {"provider": "platform", "version": 1}})
    parsed = json.loads(raw)
    assert parsed["provider"] == "platform"
    assert "query_model" not in parsed


def test_normalize_put_device_overrides_requires_model_for_ollama() -> None:
    with pytest.raises(ValueError, match="query_model"):
        normalize_put_device_overrides({"device_overrides": {"provider": "ollama", "version": 1}})
