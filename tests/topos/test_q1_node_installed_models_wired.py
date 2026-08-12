"""Q1 — the node's pack readers ask what is actually installed.

Node mirror of the CP guard: `resolve_model(..., installed_local_models=...)`
shipped in S6 with no caller (PLAN_LOCAL_MODEL_QUICKSTART §1.4), so an active
pack binding `classify` to a tag the machine never pulled was trusted until the
Ollama call 404'd mid-batch. The readers must supply the live list, and the
fallback must land on the node's own default — never off the machine.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from topos.config import model_packs
from topos.config.conversation_context_llm import resolve_context_llm_model
from topos.config.facts_llm import resolve_facts_llm_model
from topos.config.model_packs import apply_sync_payload

NODE_ROOT = Path(__file__).resolve().parents[2]

MISSING_TAG = "qwen3.5:9b-mlx"
INSTALLED_TAG = "llama3.2:latest"

#: Captured before the autouse fixture below stubs the module attribute, so the
#: probe's own tests can exercise the real implementation.
REAL_INSTALLED_PROBE = model_packs.installed_local_models


class _Settings:
    facts_llm_model = ""
    ollama_extraction_model = "extraction-default:latest"
    ollama_query_model = "query-default:latest"


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
            "packs": [
                {"pack_id": "pack-1", "roles": {"classify": {"provider": provider, "model": model}}}
            ],
        },
    )


@pytest.fixture(autouse=True)
def _no_live_ollama_probe(monkeypatch):
    """Default every test to a known installed set; no test may hit the network."""
    monkeypatch.setattr(model_packs, "installed_local_models", lambda: {INSTALLED_TAG})


def test_facts_llm_falls_to_the_engine_default_when_the_pack_tag_is_not_installed():
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)
    assert resolve_facts_llm_model(_Settings(), conn) == "extraction-default:latest"


def test_facts_llm_keeps_the_pack_tag_when_it_is_installed(monkeypatch):
    monkeypatch.setattr(model_packs, "installed_local_models", lambda: {INSTALLED_TAG, MISSING_TAG})
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)
    assert resolve_facts_llm_model(_Settings(), conn) == MISSING_TAG


def test_facts_llm_still_trusts_the_pack_when_the_installed_set_is_unknown(monkeypatch):
    # Ollama down ⇒ "unknown", not "empty". Demoting every pack role because a
    # probe failed would change behaviour on a transient hiccup.
    monkeypatch.setattr(model_packs, "installed_local_models", lambda: None)
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)
    assert resolve_facts_llm_model(_Settings(), conn) == MISSING_TAG


def test_conversation_context_falls_to_the_engine_default_when_the_tag_is_missing():
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)
    assert resolve_context_llm_model(_Settings(), conn) == "extraction-default:latest"


def test_installed_probe_reports_unknown_when_ollama_is_unreachable(monkeypatch):
    class _Down:
        def is_reachable(self, **_kwargs):
            return False

        def list_models(self):
            return []

    monkeypatch.setattr(model_packs, "OllamaAdapter", lambda *a, **k: _Down())
    model_packs.reset_installed_local_models_cache()
    assert REAL_INSTALLED_PROBE() is None


def test_installed_probe_reports_empty_when_ollama_is_up_with_nothing_pulled(monkeypatch):
    class _Empty:
        def is_reachable(self, **_kwargs):
            return True

        def list_models(self):
            return []

    monkeypatch.setattr(model_packs, "OllamaAdapter", lambda *a, **k: _Empty())
    model_packs.reset_installed_local_models_cache()
    assert REAL_INSTALLED_PROBE() == set()


def test_installed_probe_is_cached_so_every_batch_row_does_not_hit_the_socket(monkeypatch):
    calls = []

    class _Counting:
        def is_reachable(self, **_kwargs):
            return True

        def list_models(self):
            calls.append(1)
            return [INSTALLED_TAG]

    monkeypatch.setattr(model_packs, "OllamaAdapter", lambda *a, **k: _Counting())
    model_packs.reset_installed_local_models_cache()
    for _ in range(5):
        assert REAL_INSTALLED_PROBE() == {INSTALLED_TAG}
    assert len(calls) == 1


def _resolve_model_calls(relative: str) -> list[ast.Call]:
    tree = ast.parse((NODE_ROOT / relative).read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "resolve_model"
    ]


@pytest.mark.parametrize(
    "relative",
    [
        "topos/config/facts_llm.py",
        "topos/config/conversation_context_llm.py",
        "topos/config/signal_extraction.py",
    ],
)
def test_every_node_pack_reader_supplies_the_installed_set(relative):
    calls = _resolve_model_calls(relative)
    assert calls, f"{relative} no longer resolves through the node resolver"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "installed_local_models" in kwargs, (
            f"resolve_model at {relative}:{call.lineno} trusts a pack tag without "
            "checking the machine has it"
        )
