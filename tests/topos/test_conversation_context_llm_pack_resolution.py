"""PLAN_MODEL_PACKS.md M3: conversation-context classifier model resolution
consults the active pack.

Resolution order, first non-empty wins: device override -> active pack's
`classify` role (ollama-only, since this module has no cloud path) -> the
existing settings env chain.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.config.conversation_context_llm import resolve_context_llm_model
from topos.config.model_packs import apply_sync_payload


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


def _seed_pack(conn: sqlite3.Connection, *, provider: str, model: str) -> None:
    apply_sync_payload(
        conn,
        {
            "revision": 1,
            "active": "pack-1",
            "packs": [{"pack_id": "pack-1", "roles": {"classify": {"provider": provider, "model": model}}}],
        },
    )


class _Settings:
    ollama_extraction_model = "extraction-default:latest"
    ollama_query_model = "query-default:latest"


def test_the_pack_is_IGNORED_these_are_ingest_functions():
    """Inverted 2026-08-15 with the `classify` role's retirement: a query-time
    pack must not steer an ingest model. The seeded pack below names a model
    the resolver must now walk straight past, to the settings default."""
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model="phi3:latest")
    assert resolve_context_llm_model(_Settings(), conn) == _Settings().ollama_extraction_model


def test_device_override_wins_over_pack():
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model="phi3:latest")
    conn.execute(
        "INSERT INTO engine_config (key, value) VALUES ('conversation_context_llm_model', 'device-pin:latest')"
    )
    conn.commit()
    assert resolve_context_llm_model(_Settings(), conn) == "device-pin:latest"


def test_falls_through_when_pack_binds_a_non_ollama_provider():
    conn = _memory_conn()
    _seed_pack(conn, provider="openai", model="gpt-4o-mini")
    assert resolve_context_llm_model(_Settings(), conn) == "extraction-default:latest"


def test_no_connection_falls_back_to_settings_chain():
    assert resolve_context_llm_model(_Settings(), None) == "extraction-default:latest"
