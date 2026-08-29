"""llm_generation Ollama path must suppress thinking for non-stream calls."""

from __future__ import annotations

import json

import httpx
import pytest

from topos.services.llm import openai as llm_openai


class _Resp:
    def __init__(self, status_code: int, payload: dict | str):
        self.status_code = status_code
        if isinstance(payload, dict):
            self._text = json.dumps(payload)
            self._json = payload
        else:
            self._text = str(payload)
            self._json = {}

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        return self._json


@pytest.mark.asyncio
async def test_nonstream_ollama_generate_sends_think_false(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            captured["url"] = url
            captured["body"] = json
            return _Resp(
                200,
                {
                    "response": "digest ok",
                    "model": "qwen3.5:9b-mlx",
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            )

    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_generate_timeout_sec", 300.0)
    monkeypatch.setattr(llm_openai.settings, "sanitization_ollama_default_model", "llama3.2")
    monkeypatch.setattr(llm_openai, "_ensure_ollama_running", lambda _base=None: None)

    out = await llm_openai._ollama_generate(
        {"prompt": "write digest", "model": "qwen3.5:9b-mlx", "max_tokens": 400}
    )
    assert out["output"] == "digest ok"
    assert captured["body"]["think"] is False
    assert float(captured["timeout"].read) == 300.0


@pytest.mark.asyncio
async def test_nonstream_retries_when_model_rejects_think_false(monkeypatch):
    posts = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            # Copy — the production path mutates the same body dict on retry.
            posts.append(dict(json or {}))
            if json and json.get("think") is False:
                return _Resp(400, 'qwen does not support disabling thinking')
            return _Resp(
                200,
                {"response": "ok", "model": "gpt-oss", "prompt_eval_count": 1, "eval_count": 1},
            )

    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(llm_openai.settings, "sanitization_ollama_default_model", "llama3.2")
    monkeypatch.setattr(llm_openai, "_ensure_ollama_running", lambda _base=None: None)

    out = await llm_openai._ollama_generate({"prompt": "hi", "model": "gpt-oss"})
    assert out["output"] == "ok"
    assert len(posts) == 2
    assert posts[0].get("think") is False
    assert "think" not in posts[1]
