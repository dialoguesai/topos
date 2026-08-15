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


def _seed_pack(conn: sqlite3.Connection, *, provider: str, model: str, role: str = "classify") -> None:
    apply_sync_payload(
        conn,
        {
            "revision": 1,
            "active": "pack-1",
            "packs": [
                {"pack_id": "pack-1", "roles": {role: {"provider": provider, "model": model}}}
            ],
        },
    )


@pytest.fixture(autouse=True)
def _no_live_ollama_probe(monkeypatch):
    """Default every test to a known installed set; no test may hit the network."""
    monkeypatch.setattr(model_packs, "installed_local_models", lambda: {INSTALLED_TAG})


def test_facts_llm_falls_to_the_engine_default_with_a_pack_present():
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)
    assert resolve_facts_llm_model(_Settings(), conn) == "extraction-default:latest"


def test_facts_llm_ignores_the_pack_entirely(monkeypatch):
    """Inverted 2026-08-15 with the `classify` role's retirement.

    The two tests this replaces pinned the pack rung's behaviour (installed tag
    kept; unknown installed-set trusted). There is no pack rung to pin any more:
    facts extraction is an ingest function whose model comes from Node functions
    or the settings chain, and a query pack naming even an INSTALLED tag must be
    walked past. The 404-mid-batch hazard those tests guarded cannot occur when
    no pack tag can be chosen.
    """
    monkeypatch.setattr(model_packs, "installed_local_models", lambda: {INSTALLED_TAG, MISSING_TAG})
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=INSTALLED_TAG)
    assert resolve_facts_llm_model(_Settings(), conn) == "extraction-default:latest"


def test_conversation_context_falls_to_the_engine_default_with_a_pack_present():
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


# --------------------------------------------------------------------------
# The mirrored demoted-local branch, exercised the way production reaches it
# --------------------------------------------------------------------------


class _NoDefaults:
    """A node that never had a fallback model configured.

    This is the only shape that reaches the node's demoted-local branch, and it
    is why the branch is not dead code: every other node `engine_default` is an
    ollama binding, which is already on the right side of the locality line.
    """

    facts_llm_model = ""
    ollama_extraction_model = ""
    ollama_query_model = ""


def test_a_missing_local_tag_with_no_engine_default_goes_inert_rather_than_off_the_box():
    # Nothing on this machine can serve the role and there is no configured
    # fallback. The pass staying inert is the correct answer; anything that
    # names a model here would either 404 on first use or be a provider the
    # owner never pinned.
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG)

    assert resolve_facts_llm_model(_NoDefaults(), conn) == ""


def test_the_node_resolver_keeps_a_demoted_local_role_on_the_local_provider():
    resolved = model_packs.resolve_model(
        role="tool",
        pack={
            "pack_id": "pack-1",
            "roles": {"tool": {"provider": "ollama", "model": MISSING_TAG}},
        },
        engine_default=None,
        installed_local_models={INSTALLED_TAG},
    )

    assert resolved.provider == "ollama", (
        f"a role pinned to this machine was demoted to {resolved.provider!r}; an "
        "empty provider hands the turn to the cloud routers, which answers a "
        "failed download by moving the owner's data off their machine"
    )
    assert resolved.model == "", "the missing tag was kept, so the call still 404s"


def test_a_cloud_engine_default_does_not_rescue_a_demoted_local_role():
    # The mirror's whole point: the fallback being *configured* is not enough,
    # it has to be on the same side of the locality line.
    resolved = model_packs.resolve_model(
        role="tool",
        pack={
            "pack_id": "pack-1",
            "roles": {"tool": {"provider": "ollama", "model": MISSING_TAG}},
        },
        engine_default={"provider": "openai", "model": "gpt-4o-mini"},
        installed_local_models={INSTALLED_TAG},
    )

    assert resolved.provider == "ollama"
    assert resolved.model != "gpt-4o-mini"


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
        # facts_llm and conversation_context_llm left this list 2026-08-15: they
        # are ingest functions and no longer read packs at all (their absence is
        # asserted separately below).
        "topos/config/signal_extraction.py",
    ],
)
def test_every_node_pack_reader_supplies_the_installed_set(relative):
    calls = _resolve_model_calls(relative)
    assert calls, f"{relative} no longer resolves through the node resolver"
    for call in calls:
        kw_map = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        assert "installed_local_models" in kw_map, (
            f"resolve_model at {relative}:{call.lineno} trusts a pack tag without "
            "checking the machine has it"
        )
        value = kw_map["installed_local_models"]
        # `installed_local_models=None` recreates the dead parameter; a bare
        # name-presence check cannot see it. The value must call the probe
        # (or name a variable bound to it).
        live = False
        if isinstance(value, ast.Call):
            func = value.func
            live = (isinstance(func, ast.Name) and func.id == "installed_local_models") or (
                isinstance(func, ast.Attribute) and func.attr == "installed_local_models"
            )
        elif isinstance(value, ast.Name):
            live = value.id in {"installed", "installed_local_models"}
        assert live, (
            f"resolve_model at {relative}:{call.lineno} passes {ast.dump(value)}, "
            "not the live installed set — the router never sees what the machine has"
        )


def test_signal_extraction_falls_to_the_engine_default_when_the_pack_tag_is_missing():
    """Behavioural twin of the facts_llm guard — AST presence is not enough."""
    from topos.config.signal_extraction import resolve_signal_extraction_config

    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG, role="tool")
    resolved = resolve_signal_extraction_config(_Settings(), conn)
    assert resolved.query_model == "extraction-default:latest", (
        f"signal extraction kept the missing pack tag {resolved.query_model!r} "
        "instead of falling to the engine default"
    )


def test_signal_extraction_keeps_the_pack_tag_when_it_is_installed(monkeypatch):
    from topos.config.signal_extraction import resolve_signal_extraction_config

    monkeypatch.setattr(model_packs, "installed_local_models", lambda: {INSTALLED_TAG, MISSING_TAG})
    conn = _memory_conn()
    _seed_pack(conn, provider="ollama", model=MISSING_TAG, role="tool")
    resolved = resolve_signal_extraction_config(_Settings(), conn)
    assert resolved.query_model == MISSING_TAG


def test_ingest_functions_do_not_read_packs_at_all():
    """The inverse of the parametrized wiring test: these two must have NO
    resolve_model call. Re-adding one would put a query-time pack back in
    charge of an ingest model."""
    for relative in ("topos/config/facts_llm.py", "topos/config/conversation_context_llm.py"):
        assert _resolve_model_calls(relative) == [], (
            f"{relative} resolves through the pack resolver again — ingest "
            "functions are configured under Node functions, not by packs"
        )
