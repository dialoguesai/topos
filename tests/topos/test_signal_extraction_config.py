"""Tests for signal extraction model config resolution."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.config.model_packs import apply_sync_payload
from topos.config.settings import Settings
from topos.config.signal_extraction import (
    ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
    effective_config_for_api,
    normalize_put_device_overrides,
    resolve_signal_extraction_config,
    resolve_signal_extraction_model_request,
    resolve_signal_extraction_query_model,
)


# Deliberately NOT the field defaults ("llama3.2:latest" / "qwen3.5:9b-mlx"):
# these tests assert env values reach Settings, so a sentinel that collides with
# the default would pass even if env resolution were completely broken.
ENV_QUERY_MODEL = "env-query-model:test"
ENV_EXTRACTION_MODEL = "env-extraction-model:test"


@pytest.fixture()
def memory_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOPOS_KEY", "test-key-for-settings")
    monkeypatch.setenv("TOPOS_OLLAMA_QUERY_MODEL", ENV_QUERY_MODEL)
    monkeypatch.setenv("TOPOS_OLLAMA_EXTRACTION_MODEL", ENV_EXTRACTION_MODEL)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    return Settings(_env_file=None)


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


def test_documented_env_names_reach_settings(memory_settings: Settings) -> None:
    """The TOPOS_-prefixed names the docs and backfill scripts use must bind.

    Regression guard: these were declared as Field(env="TOPOS_..."), which
    pydantic v2 silently ignores, so the documented names were dead and the
    fields only ever read the bare OLLAMA_* forms.
    """
    assert memory_settings.ollama_query_model == ENV_QUERY_MODEL
    assert memory_settings.ollama_extraction_model == ENV_EXTRACTION_MODEL


def test_resolve_signal_extraction_query_model_uses_env_default(memory_settings: Settings) -> None:
    conn = _memory_conn()
    # C1 (5407bf5): ingest extraction prefers the QUALITY model — the resolver
    # reads settings.ollama_extraction_model first, falling back to the query model.
    # Asserted against the literal sentinel so this cannot pass vacuously if
    # either the env binding or the extraction-over-query preference regresses.
    assert resolve_signal_extraction_query_model(memory_settings, conn) == ENV_EXTRACTION_MODEL


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
    assert payload["defaults_from_settings"]["query_model"] == memory_settings.ollama_query_model
    assert payload["effective"]["provider"] == "ollama"
    assert payload["effective"]["query_model"] == memory_settings.ollama_extraction_model


def test_normalize_put_device_overrides_platform_without_model(memory_settings: Settings) -> None:
    raw = normalize_put_device_overrides({"device_overrides": {"provider": "platform", "version": 1}})
    parsed = json.loads(raw)
    assert parsed["provider"] == "platform"
    assert "query_model" not in parsed


def test_normalize_put_device_overrides_requires_model_for_ollama() -> None:
    with pytest.raises(ValueError, match="query_model"):
        normalize_put_device_overrides({"device_overrides": {"provider": "ollama", "version": 1}})


# --- PLAN_MODEL_PACKS.md M3: device override -> pack's `tool` role -> default -------


def _seed_pack(conn: sqlite3.Connection, *, provider: str, model: str) -> None:
    apply_sync_payload(
        conn,
        {
            "revision": 1,
            "active": "pack-1",
            "packs": [{"pack_id": "pack-1", "roles": {"tool": {"provider": provider, "model": model}}}],
        },
    )


def test_resolve_signal_extraction_config_uses_pack_tool_role_when_no_device_override(
    memory_settings: Settings,
) -> None:
    conn = _memory_conn()
    _seed_pack(conn, provider="redpill", model="qwen/qwen3.6-27b")
    cfg = resolve_signal_extraction_config(memory_settings, conn)
    assert cfg.provider == "redpill"
    assert cfg.query_model == "qwen/qwen3.6-27b"


def test_resolve_signal_extraction_config_device_override_wins_over_pack(memory_settings: Settings) -> None:
    conn = _memory_conn()
    _seed_pack(conn, provider="redpill", model="qwen/qwen3.6-27b")
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (
            ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
            json.dumps({"version": 1, "provider": "ollama", "query_model": "mistral:latest"}),
        ),
    )
    conn.commit()
    cfg = resolve_signal_extraction_config(memory_settings, conn)
    assert cfg.provider == "ollama"
    assert cfg.query_model == "mistral:latest"


def test_resolve_signal_extraction_config_falls_through_for_unsupported_pack_provider(
    memory_settings: Settings,
) -> None:
    conn = _memory_conn()
    # anthropic/grok aren't in SIGNAL_EXTRACTION_PROVIDERS -- must fall through
    # to this function's own default rather than erroring or using them.
    _seed_pack(conn, provider="anthropic", model="claude-opus-4-6")
    cfg = resolve_signal_extraction_config(memory_settings, conn)
    assert cfg.provider == "ollama"
    assert cfg.query_model == memory_settings.ollama_extraction_model
