"""Thinking-capability auto-adaptation in the Ollama adapter.

Pins the contract that lets ANY owner-selected model run structured lanes
(facts, goals, …) safely:

  * /api/show capability probe, cached per (base_url, model).
  * resolve_think: omit ``think`` for non-thinking models (some Ollama versions
    400 on it), pass the caller's intent through for thinking models.
  * think=False callers on a model that REJECTS disabling ("does not support
    disabling thinking", gpt-oss family) fall back to thinking-enabled with a
    widened num_predict — remembered, so only the first call pays the retry.
  * HTTP error bodies are surfaced (Ollama puts the actionable message there).

All network is faked at urllib.request.urlopen; no Ollama required.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from topos.engine.backends import ollama as ollama_mod
from topos.engine.backends.ollama import (
    _THINK_DISABLE_REJECTED,
    _THINKING_CAPABILITY_CACHE,
    _THINKING_NUM_PREDICT_FLOOR,
    OllamaAdapter,
)

BASE = "http://fake-ollama:11434"


@pytest.fixture(autouse=True)
def _clean_caches():
    _THINKING_CAPABILITY_CACHE.clear()
    _THINK_DISABLE_REJECTED.clear()
    yield
    _THINKING_CAPABILITY_CACHE.clear()
    _THINK_DISABLE_REJECTED.clear()


class _FakeResp:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOllama:
    """Routes urlopen calls; records every /api/generate body."""

    def __init__(self, capabilities_by_model, *, reject_think_false=frozenset()):
        self.capabilities_by_model = dict(capabilities_by_model)
        self.reject_think_false = set(reject_think_false)
        self.generate_bodies: list[dict] = []
        self.show_calls = 0

    def __call__(self, req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        if url.endswith("/api/show"):
            self.show_calls += 1
            model = body.get("model")
            if model not in self.capabilities_by_model:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b"{}"))
            return _FakeResp({"capabilities": self.capabilities_by_model[model]})
        if url.endswith("/api/generate"):
            self.generate_bodies.append(body)
            if body.get("model") in self.reject_think_false and body.get("think") is False:
                detail = json.dumps(
                    {"error": f'"{body.get("model")}" does not support disabling thinking'}
                ).encode("utf-8")
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, io.BytesIO(detail))
            return _FakeResp({"response": "[]", "prompt_eval_count": 7, "eval_count": 1})
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture()
def adapter():
    return OllamaAdapter(base_url=BASE)


def _install(monkeypatch, fake):
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def test_probe_detects_thinking_and_caches(monkeypatch, adapter):
    fake = _install(monkeypatch, _FakeOllama({"qwen": ["completion", "thinking"]}))
    assert adapter.model_supports_thinking("qwen") is True
    assert adapter.model_supports_thinking("qwen") is True
    assert fake.show_calls == 1  # second answer came from the cache


def test_probe_detects_non_thinking(monkeypatch, adapter):
    _install(monkeypatch, _FakeOllama({"llama": ["completion", "tools"]}))
    assert adapter.model_supports_thinking("llama") is False


def test_probe_failure_returns_false_but_is_not_cached(monkeypatch, adapter):
    def _down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    assert adapter.model_supports_thinking("qwen") is False
    # Server comes back: the next probe re-asks instead of trusting a bad cache.
    fake = _install(monkeypatch, _FakeOllama({"qwen": ["thinking"]}))
    assert adapter.model_supports_thinking("qwen") is True
    assert fake.show_calls == 1


def test_resolve_think_passthrough_and_gating(monkeypatch, adapter):
    _install(
        monkeypatch,
        _FakeOllama({"qwen": ["thinking"], "llama": ["completion"]}),
    )
    assert adapter.resolve_think("qwen", None) is None
    assert adapter.resolve_think("qwen", False) is False
    assert adapter.resolve_think("qwen", True) is True
    assert adapter.resolve_think("llama", False) is None
    assert adapter.resolve_think("llama", True) is None


def test_generate_omits_think_for_non_thinking_model(monkeypatch, adapter):
    fake = _install(monkeypatch, _FakeOllama({"llama": ["completion"]}))
    adapter._generate("llama", "p", num_predict=64, think=False)
    (body,) = fake.generate_bodies
    assert "think" not in body
    assert body["options"]["num_predict"] == 64


def test_generate_sends_think_false_for_thinking_model(monkeypatch, adapter):
    fake = _install(monkeypatch, _FakeOllama({"qwen": ["thinking"]}))
    adapter._generate("qwen", "p", num_predict=64, think=False)
    (body,) = fake.generate_bodies
    assert body["think"] is False
    assert body["options"]["num_predict"] == 64


def test_disable_rejected_falls_back_and_is_remembered(monkeypatch, adapter):
    fake = _install(
        monkeypatch,
        _FakeOllama({"gptoss": ["thinking"]}, reject_think_false={"gptoss"}),
    )
    out = adapter._generate("gptoss", "p", num_predict=64, think=False)
    assert out["text"] == "[]"
    # First call: rejected attempt + retry without think and a widened budget.
    first, retry = fake.generate_bodies
    assert first["think"] is False
    assert "think" not in retry
    assert retry["options"]["num_predict"] == _THINKING_NUM_PREDICT_FLOOR

    fake.generate_bodies.clear()
    adapter._generate("gptoss", "p2", num_predict=64, think=False)
    # Remembered: exactly one request, no think, widened budget, no failed probe.
    (only,) = fake.generate_bodies
    assert "think" not in only
    assert only["options"]["num_predict"] == _THINKING_NUM_PREDICT_FLOOR


def test_http_error_body_is_surfaced(monkeypatch, adapter):
    def _boom(req, timeout=None):
        if req.full_url.endswith("/api/show"):
            return _FakeResp({"capabilities": ["thinking"]})
        raise urllib.error.HTTPError(
            req.full_url, 500, "Internal Server Error", None,
            io.BytesIO(b'{"error":"model requires more system memory"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="more system memory"):
        adapter._generate("qwen", "p", num_predict=8, think=False)


def test_module_constants_exist():
    # llm_extract keys its batch-halt heuristics off these; keep them stable.
    assert isinstance(ollama_mod._THINKING_NUM_PREDICT_FLOOR, int)
