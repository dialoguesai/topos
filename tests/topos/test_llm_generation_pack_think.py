"""S1 — a pack's `thinking` must survive the llm_generation boundary.

`_ollama_generate` already honours an explicit ``think`` in its payload, but the
request model in between is what routines and the control plane actually send
through. A field the model does not declare is dropped there, so a pack setting
`thinking: false` on `tool` would arrive as "nothing stated" and fall back to
the hard-coded non-stream default — which is the behaviour the pack exists to
supersede.
"""

from __future__ import annotations

import json

import pytest

from topos.core.api_models import GenerationRequest
from topos.services.llm import openai as llm_openai


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def captured(monkeypatch):
    seen: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            seen["body"] = dict(json or {})
            return _Resp(
                200,
                {"response": "ok", "model": "qwen3.5:9b-mlx", "prompt_eval_count": 1, "eval_count": 1},
            )

    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(llm_openai.settings, "sanitization_ollama_default_model", "llama3.2")
    monkeypatch.setattr(llm_openai, "_ensure_ollama_running", lambda _base=None: None)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("wanted", [True, False])
async def test_generation_request_carries_a_pack_set_think_to_ollama(captured, wanted):
    body = GenerationRequest.model_validate(
        {"prompt": "which tool?", "model": "qwen3.5:9b-mlx", "think": wanted}
    )
    # Asserted on the dump too: think=False happens to match the non-stream
    # default, so reading only the outbound body would let a model that drops
    # the field pass for half the cases.
    dumped = body.model_dump()
    assert dumped["think"] is wanted
    await llm_openai._ollama_generate(dumped)
    assert captured["body"]["think"] is wanted


@pytest.mark.asyncio
async def test_an_unstated_think_still_gets_the_non_stream_default(captured):
    """The field is always present once declared; None must mean "not stated"."""
    body = GenerationRequest.model_validate({"prompt": "hi", "model": "qwen3.5:9b-mlx"})
    dumped = body.model_dump()
    assert dumped["think"] is None
    await llm_openai._ollama_generate(dumped)
    assert captured["body"]["think"] is False
