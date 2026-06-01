"""Merge Settings + engine_config device overrides for Ollama sanitization."""

import json
import sqlite3

import pytest

from topos.config.sanitization_ollama import (
    ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
    resolve_sanitization_ollama_effective,
)
from topos.config.settings import Settings


@pytest.fixture()
def memory_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOPOS_KEY", "test-key-for-settings")
    monkeypatch.setenv("SANITIZATION_OLLAMA_ENABLED", "true")
    monkeypatch.setenv("SANITIZATION_OLLAMA_DEFAULT_MODEL", "llama3.2")
    monkeypatch.setenv("SANITIZATION_OLLAMA_MODEL_PII_REDACTION", "model-a")
    return Settings()


def test_device_overrides_per_transform_model(memory_settings: Settings) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    payload = {
        "models": {
            "pii_redaction": "phi3",
            "nsfw_sanitization": "mistral",
        }
    }
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json.dumps(payload)),
    )
    conn.commit()

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.enabled is True
    assert eff.models["pii_redaction"] == "phi3"
    assert eff.models["nsfw_sanitization"] == "mistral"
    # unset in device → still uses settings per-transform or default
    assert eff.models["name_removal"] == "llama3.2"


def test_device_disabled_overrides_settings(memory_settings: Settings) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json.dumps({"enabled": False})),
    )
    conn.commit()

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.enabled is False
