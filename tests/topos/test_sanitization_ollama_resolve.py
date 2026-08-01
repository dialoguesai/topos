"""Merge Settings + engine_config device overrides for Ollama sanitization."""

import json
import sqlite3

import pytest

from topos.config.model_packs import apply_sync_payload
from topos.config.sanitization_ollama import (
    ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
    resolve_sanitization_ollama_effective,
)
from topos.config.settings import Settings


def _memory_conn() -> sqlite3.Connection:
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
    return conn


def _seed_pack(conn: sqlite3.Connection, *, provider: str, model: str, **params: object) -> None:
    apply_sync_payload(
        conn,
        {
            "revision": 1,
            "active": "pack-1",
            "packs": [
                {
                    "pack_id": "pack-1",
                    "roles": {"tool": {"provider": provider, "model": model, **params}},
                }
            ],
        },
    )


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


# --- S1b: here a pack supplies PARAMETERS, never the model ------------------
#
# Which model redacts PII is a privacy decision with its own resolution order
# (device override, per-transform setting, then the default). An earlier round
# inserted the pack's model ahead of the setting, so merely activating a pack
# moved redaction onto a different model as a side effect. These pin the revert
# from the node side, and pin what the binding is still there to carry.


def test_an_active_pack_does_not_change_which_model_redacts(memory_settings: Settings) -> None:
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model="phi3:latest")

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.default_model == "llama3.2"
    assert eff.models["pii_redaction"] == "model-a"
    assert eff.models["name_removal"] == "llama3.2"


def test_an_active_ollama_pack_is_still_carried_for_its_parameters(memory_settings: Settings) -> None:
    """The revert dropped the pack's model, not its knobs -- `thinking` and
    `context` still have to reach the transform, and only on their own model."""
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model="phi3:latest", thinking=True, context=4096)

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.pack is not None
    assert eff.params_for("phi3:latest").thinking is True
    assert eff.params_for("phi3:latest").context == 4096
    # The model that actually redacts here is `llama3.2`; knobs their owner
    # attached to phi3 must not follow the run onto it.
    assert eff.params_for("llama3.2") is None


def test_device_default_model_wins_and_a_pack_does_not_interfere(memory_settings: Settings) -> None:
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model="phi3:latest")
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES (?, ?)",
        (ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json.dumps({"default_model": "mistral:latest"})),
    )
    conn.commit()

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.default_model == "mistral:latest"


def test_a_non_ollama_pack_contributes_neither_model_nor_parameters(memory_settings: Settings) -> None:
    conn = _memory_conn()
    # Sanitization never leaves the local machine, so a `tool` role bound to a
    # cloud provider describes a different run entirely: neither its model nor
    # its knobs apply to the local one that does the redaction.
    _seed_pack(conn, provider="openai", model="gpt-4o-mini", thinking=True)

    eff = resolve_sanitization_ollama_effective(memory_settings, conn)
    assert eff.default_model == "llama3.2"
    assert eff.pack is None
