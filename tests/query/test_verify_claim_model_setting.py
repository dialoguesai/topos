"""The owner's verify_claim model pick (``config/verify_claim_model.py``): what a write stores, the resolution order
(pick, then ``TOPOS_VERIFY_V2_MODEL``, then the key-present default), the inbound lane's ``hosted``/``local`` through
the same pick, and the three owner-door types. Synthetic only: an in-memory ``engine_config`` or a migrated temp
database; never the live node."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from topos.config import verify_claim_model as cfg
from topos.query import verify_v2_models as m

HOSTED_120B = "openai/gpt-oss-120b"
LLAMA_70B = "meta-llama/llama-3.3-70b-instruct"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)", (key, value))


def _store(conn: sqlite3.Connection, provider: str, model: str) -> None:
    _set(conn, cfg.ENGINE_CONFIG_KEY_VERIFY_CLAIM_PROVIDER, provider)
    _set(conn, cfg.ENGINE_CONFIG_KEY_VERIFY_CLAIM_MODEL, model)


@pytest.fixture
def clean_env(monkeypatch):
    for name in (cfg.ENV_CHOICE, cfg.ENV_LOCAL_MODEL, "TOPOS_VERIFY_V2_SECOND_JUDGE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def chats(monkeypatch):
    """HostedChat and LocalChat keep only their model: no settings, no network."""
    monkeypatch.setattr(m.HostedChat, "__init__",
                        lambda self, model=None, **k: (setattr(self, "model", model or m.DEFAULT_HOSTED_MODEL),
                                                       setattr(self, "_key", "k")) and None)
    monkeypatch.setattr(m.LocalChat, "__init__",
                        lambda self, model=None, **k: setattr(self, "model", model or m.DEFAULT_LOCAL_MODEL))


# --- the write ---------------------------------------------------------------------------------------------------------
class TestNormalizePutConfig:
    def test_local_pick_stores_the_legacy_shape(self):
        assert cfg.normalize_put_config({"model": "qwen3.5:9b-mlx"}) == ("", "qwen3.5:9b-mlx")
        assert cfg.normalize_put_config({"provider": "ollama", "model": "qwen3.5:9b-mlx"}) == ("", "qwen3.5:9b-mlx")
        assert cfg.normalize_put_config("llama3.2:3b") == ("", "llama3.2:3b")

    def test_hosted_pick_must_be_a_model_this_node_runs(self):
        assert cfg.normalize_put_config({"provider": "redpill", "model": HOSTED_120B}) == ("redpill", HOSTED_120B)
        assert cfg.normalize_put_config({"provider": "redpill", "model": LLAMA_70B}) == ("redpill", LLAMA_70B)
        with pytest.raises(ValueError, match="hosted model not available"):
            cfg.normalize_put_config({"provider": "redpill", "model": "vendor/not-a-routed-model"})

    def test_clears(self):
        assert cfg.normalize_put_config(None) == ("", "")
        assert cfg.normalize_put_config({"model": ""}) == ("", "")
        assert cfg.normalize_put_config({"provider": "redpill"}) == ("", "")

    def test_garbage_is_refused(self):
        with pytest.raises(ValueError):
            cfg.normalize_put_config({"provider": "platform", "model": "gpt-4o-mini"})
        with pytest.raises(ValueError):
            cfg.normalize_put_config({"model": "has a space"})
        with pytest.raises(ValueError):
            cfg.normalize_put_config(["model"])


# --- the read ----------------------------------------------------------------------------------------------------------
def test_device_pick_reads_the_two_keys_and_ignores_a_half_row():
    conn = _conn()
    assert cfg.device_pick(conn) == ("", "")
    assert cfg.device_pick(None) == ("", "")
    _store(conn, "redpill", HOSTED_120B)
    assert cfg.device_pick(conn) == ("redpill", HOSTED_120B)
    _store(conn, "", "qwen3.5:9b-mlx")
    assert cfg.device_pick(conn) == ("", "qwen3.5:9b-mlx")
    _store(conn, "redpill", "")  # a provider with no model chooses nothing
    assert cfg.device_pick(conn) == ("", "")


class TestResolutionOrder:
    def test_pick_wins_over_env_and_key(self, monkeypatch, clean_env):
        monkeypatch.setenv(cfg.ENV_CHOICE, "local")
        settings = SimpleNamespace(red_pill_api_key="k")
        assert cfg.resolve_verify_claim_request(settings, ("redpill", HOSTED_120B)) == ("redpill", HOSTED_120B, "device_override")
        monkeypatch.setenv(cfg.ENV_CHOICE, "hosted")
        assert cfg.resolve_verify_claim_request(settings, ("", "qwen3.5:9b-mlx")) == ("ollama", "qwen3.5:9b-mlx", "device_override")

    def test_env_decides_when_nothing_is_picked(self, monkeypatch, clean_env):
        settings = SimpleNamespace(red_pill_api_key="k")
        monkeypatch.setenv(cfg.ENV_CHOICE, "local")
        monkeypatch.setenv(cfg.ENV_LOCAL_MODEL, "gemma4:9b")
        assert cfg.resolve_verify_claim_request(settings, ("", "")) == ("ollama", "gemma4:9b", "env")
        monkeypatch.setenv(cfg.ENV_CHOICE, "hosted")
        assert cfg.resolve_verify_claim_request(SimpleNamespace(red_pill_api_key=""), None) == ("redpill", m.DEFAULT_HOSTED_MODEL, "env")

    def test_key_presence_is_the_last_rung(self, clean_env):
        assert cfg.resolve_verify_claim_request(SimpleNamespace(red_pill_api_key="k"), None) == ("redpill", m.DEFAULT_HOSTED_MODEL, "default")
        assert cfg.resolve_verify_claim_request(SimpleNamespace(red_pill_api_key=""), None) == ("ollama", m.DEFAULT_LOCAL_MODEL, "default")


def test_effective_config_for_api_names_both_sides_and_the_catalogs(monkeypatch, clean_env):
    conn = _conn()
    _store(conn, "redpill", HOSTED_120B)
    monkeypatch.setattr(cfg, "default_local_model", lambda: "qwen3.5:9b-mlx")

    class NoOllama:
        def list_models(self):
            raise OSError("down")

    import topos.engine.backends.ollama as ollama

    monkeypatch.setattr(ollama, "OllamaAdapter", NoOllama)
    out = cfg.effective_config_for_api(SimpleNamespace(red_pill_api_key="k"), conn)
    assert (out["provider"], out["model"], out["source"]) == ("redpill", HOSTED_120B, "device_override")
    assert (out["device_override_provider"], out["device_override"]) == ("redpill", HOSTED_120B)
    assert (out["hosted_model"], out["local_model"]) == (HOSTED_120B, "qwen3.5:9b-mlx")
    assert HOSTED_120B in out["hosted_models"] and LLAMA_70B in out["hosted_models"]
    assert out["available_models"] == []  # the catalog is a nicety, never a failure


# --- what verify runs on -----------------------------------------------------------------------------------------------
class TestChatForSetting:
    def test_the_pick_names_the_backend_and_the_model(self, chats, clean_env, monkeypatch):
        monkeypatch.setenv(cfg.ENV_CHOICE, "local")  # ignored once a pick exists
        hosted = m.chat_for_setting(("redpill", HOSTED_120B))
        assert isinstance(hosted, m.HostedChat) and hosted.model == HOSTED_120B
        local = m.chat_for_setting(("", "gemma4:9b"))
        assert isinstance(local, m.LocalChat) and local.model == "gemma4:9b"

    def test_without_a_pick_the_environment_decides_as_before(self, chats, clean_env, monkeypatch):
        monkeypatch.setattr(m, "device_pick", lambda: ("", ""))
        monkeypatch.setenv(cfg.ENV_CHOICE, "local")
        assert isinstance(m.chat_for_setting(), m.LocalChat)
        monkeypatch.setenv(cfg.ENV_CHOICE, "hosted")
        assert isinstance(m.chat_for_setting(), m.HostedChat)
        monkeypatch.delenv(cfg.ENV_CHOICE)
        assert isinstance(m.chat_for_setting(), m.HostedChat)  # a key is configured (the stub sets one)
        assert m.chat_for_setting().model == m.DEFAULT_HOSTED_MODEL

    def test_device_pick_reads_this_node_and_survives_no_database(self, monkeypatch):
        import topos.core.state as state

        monkeypatch.setattr(state, "get_db_connection", lambda: None)
        assert m.device_pick() == ("", "")
        conn = _conn()
        _store(conn, "", "gemma4:9b")
        monkeypatch.setattr(state, "get_db_connection", lambda: conn)
        assert m.device_pick() == ("", "gemma4:9b")

    def test_judges_follow_the_pick_and_skip_a_duplicate_second_judge(self, chats, clean_env, monkeypatch):
        monkeypatch.setenv("TOPOS_VERIFY_V2_SECOND_JUDGE", m.SECOND_JUDGE_MODEL)
        assert [j.model for j in m.judges_for_setting(("redpill", LLAMA_70B))] == [LLAMA_70B, m.SECOND_JUDGE_MODEL]
        assert [j.model for j in m.judges_for_setting(("redpill", m.SECOND_JUDGE_MODEL))] == [m.SECOND_JUDGE_MODEL]
        assert [type(j).__name__ for j in m.judges_for_setting(("", "gemma4:9b"))] == ["LocalChat"]


class TestLaneMapping:
    """``truth_inbound.model`` chooses a side; the pick says which model that side is."""

    def test_hosted_side(self, chats, clean_env):
        assert m.chat_for_lane("hosted", ("redpill", HOSTED_120B)).model == HOSTED_120B
        assert m.chat_for_lane("hosted", ("", "gemma4:9b")).model == m.DEFAULT_HOSTED_MODEL   # a local pick: hosted default
        assert m.chat_for_lane("hosted", ("", "")).model == m.DEFAULT_HOSTED_MODEL
        assert all(isinstance(m.chat_for_lane("hosted", p), m.HostedChat) for p in (("redpill", HOSTED_120B), ("", "")))

    def test_local_side(self, chats, clean_env, monkeypatch):
        assert m.chat_for_lane("local", ("", "gemma4:9b")).model == "gemma4:9b"
        assert m.chat_for_lane("local", ("redpill", HOSTED_120B)).model == m.DEFAULT_LOCAL_MODEL  # a hosted pick: local default
        monkeypatch.setenv(cfg.ENV_LOCAL_MODEL, "qwen3.5:4b")
        assert m.chat_for_lane("local", ("redpill", HOSTED_120B)).model == "qwen3.5:4b"
        assert isinstance(m.chat_for_lane("local", ("", "")), m.LocalChat)

    def test_the_lane_reads_the_pick_from_this_node(self, chats, clean_env, monkeypatch):
        from topos.ingestion import truth_inbound as lane

        conn = _conn()
        _store(conn, "redpill", HOSTED_120B)
        monkeypatch.setattr(lane, "_db", lambda fn, *args: fn(conn, *args))
        assert lane._chat_for("hosted").model == HOSTED_120B
        assert lane._chat_for("local").model == m.DEFAULT_LOCAL_MODEL
        _store(conn, "", "gemma4:9b")
        assert lane._chat_for("local").model == "gemma4:9b"
        assert lane._chat_for("hosted").model == m.DEFAULT_HOSTED_MODEL


# --- the three owner-door types ----------------------------------------------------------------------------------------
@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    from tests.truth_lane_support import bind, build_node, connect

    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def _ask(msg_type: str, payload=None):
    from topos.core.handlers import handle_control_plane_request

    return asyncio.run(handle_control_plane_request({"id": "r1", "type": msg_type, "payload": payload or {}}))


def test_owner_door_round_trip(node, monkeypatch, clean_env):
    import topos.engine.backends.ollama as ollama

    class Catalog:
        def list_models(self):
            return ["qwen3.5:9b-mlx"]

        def model_supports_thinking(self, name):
            return False

    monkeypatch.setattr(ollama, "OllamaAdapter", Catalog)
    monkeypatch.setattr(cfg, "_hosted_key_configured", lambda settings: False)

    before = _ask("get_verify_claim_model_config")
    assert before["status"] == "ok"
    assert (before["payload"]["provider"], before["payload"]["source"]) == ("ollama", "default")
    assert before["payload"]["available_models"] == [{"name": "qwen3.5:9b-mlx", "supports_thinking": False}]

    put = _ask("put_verify_claim_model_config", {"provider": "redpill", "model": HOSTED_120B})
    assert put["status"] == "ok"
    assert (put["payload"]["provider"], put["payload"]["model"], put["payload"]["source"]) == ("redpill", HOSTED_120B, "device_override")
    assert cfg.device_pick(node.conn) == ("redpill", HOSTED_120B)

    refused = _ask("put_verify_claim_model_config", {"provider": "redpill", "model": "vendor/not-a-routed-model"})
    assert refused["status"] == "error" and "hosted model not available" in refused["error"]
    assert cfg.device_pick(node.conn) == ("redpill", HOSTED_120B)  # a refused write changes nothing

    cleared = _ask("delete_verify_claim_model_config")
    assert cleared["status"] == "ok" and cleared["payload"]["source"] == "default"
    assert cfg.device_pick(node.conn) == ("", "")


def test_the_write_and_the_clear_are_dangerous_doors_and_the_read_is_open():
    import topos.core.handlers as hub

    assert "get_verify_claim_model_config" in hub.owner_door_open_types()
    assert {"put_verify_claim_model_config", "delete_verify_claim_model_config"} <= hub.DANGEROUS_TYPES

    from topos.owner_door_routes import DANGEROUS, READ, lookup  # the HTTP census names both mutating routes

    assert lookup("PUT", "/v1/verify-claim-model-config").verdict == DANGEROUS
    assert lookup("DELETE", "/v1/verify-claim-model-config").verdict == DANGEROUS
    assert lookup("GET", "/v1/verify-claim-model-config").verdict == READ
