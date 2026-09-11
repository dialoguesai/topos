"""A failed Ollama generation must leave a line in node.log.

Guards the maintenance-sleep incident shape: a Mac that slept mid-generation
left no trace on the node, because every failure branch in the Ollama service
raised without logging, and a read timeout surfaced as "Ollama unreachable at
...: " with an EMPTY cause (``str(httpx.ReadTimeout(''))`` is ``''``).

Each failure line carries ``elapsed_s`` (monotonic) and ``wall_s`` (wall
clock); on macOS the monotonic clock stops during sleep, so their gap is the
time asleep. No line may carry the prompt, the response or an Ollama body.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx
import pytest
from fastapi import HTTPException

from topos.services.llm import openai as llm_openai

SENTINEL = "SENTINEL-PROMPT-BODY-4b1e"
_LOGGER = "topos.services.llm"


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


def _patch_settings(monkeypatch) -> None:
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_generate_timeout_sec", 300.0)
    monkeypatch.setattr(llm_openai.settings, "sanitization_ollama_default_model", "llama3.2")
    monkeypatch.setattr(llm_openai, "_ensure_ollama_running", lambda _base=None: None)


def _patch_post(monkeypatch, post) -> None:
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            return await post(url, json)

    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _Client)
    _patch_settings(monkeypatch)


class _FakeAdapter:
    def resolve_think(self, model: str, desired):
        return desired


def _patch_stream(monkeypatch, enter) -> None:
    """``client.stream(...)`` whose response is whatever ``enter()`` returns or raises."""

    class _StreamCM:
        async def __aenter__(self):
            return await enter()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):  # noqa: A002
            return _StreamCM()

    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(llm_openai, "_stream_ollama_adapter", lambda base: _FakeAdapter())
    _patch_settings(monkeypatch)


def _failing(monkeypatch, *, stream: bool, exc: BaseException):
    """Make one generation path's transport raise ``exc``; return that path."""

    async def fail(*_a):
        raise exc

    if stream:
        _patch_stream(monkeypatch, fail)
        return llm_openai._ollama_stream_generate
    _patch_post(monkeypatch, fail)
    return llm_openai._ollama_generate


@pytest.mark.asyncio
async def test_read_timeout_is_logged_and_named_in_the_detail(monkeypatch, caplog):
    async def post(url, body):
        raise httpx.ReadTimeout("")

    _patch_post(monkeypatch, post)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await llm_openai._ollama_generate({"prompt": SENTINEL, "model": "m"})

    assert raised.value.status_code == 502
    # The old detail was "Ollama unreachable at <base>: " -- pointing at a
    # down server when Ollama was up and simply had not answered in time.
    assert "300s" in raised.value.detail
    assert "ReadTimeout" in raised.value.detail
    assert "unreachable" not in raised.value.detail
    assert "Ollama generate timed out" in caplog.text
    assert "exc=ReadTimeout" in caplog.text
    assert "timeout_s=300" in caplog.text
    assert "model='m'" in caplog.text
    assert "stream=False" in caplog.text
    assert "elapsed_s=" in caplog.text
    assert "wall_s=" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_http_error_status_is_logged_without_the_body(monkeypatch, caplog):
    async def post(url, body):
        return _Resp(500, f"model runner crashed: {SENTINEL}")

    _patch_post(monkeypatch, post)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await llm_openai._ollama_generate({"prompt": SENTINEL, "model": "m"})

    assert raised.value.status_code == 502
    assert raised.value.detail.startswith("Ollama error:")
    assert "Ollama generate error" in caplog.text
    assert "status=500" in caplog.text
    assert "elapsed_s=" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "exc_type, text, expected_tail",
    [
        (httpx.ConnectError, "refused", "refused"),
        # An empty transport error must still say what it was.
        (httpx.RemoteProtocolError, "", "RemoteProtocolError"),
    ],
)
async def test_transport_failure_keeps_the_unreachable_prefix(
    monkeypatch, caplog, stream, exc_type, text, expected_tail
):
    generate = _failing(monkeypatch, stream=stream, exc=exc_type(text))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await generate({"prompt": SENTINEL, "model": "m"})

    assert raised.value.status_code == 502
    # The control plane's e2e skip keys on this prefix for a down Ollama.
    assert raised.value.detail.startswith("Ollama unreachable at")
    assert raised.value.detail.endswith(expected_tail)
    assert "Ollama generate failed" in caplog.text
    assert f"stream={stream}" in caplog.text
    assert f"exc={exc_type.__name__}" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_connect_timeout_is_unreachable_not_slow(monkeypatch, caplog, stream):
    # A LAN Ollama that is off or asleep, or a firewall dropping the SYN:
    # httpx gives up after the 10s connect budget. ConnectTimeout is also a
    # TimeoutException, so it was reported as a model that "did not answer
    # within 300s" -- a missing host dressed up as a slow one.
    generate = _failing(monkeypatch, stream=stream, exc=httpx.ConnectTimeout(""))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await generate({"prompt": SENTINEL, "model": "m"})

    detail = raised.value.detail
    assert raised.value.status_code == 502
    assert detail.startswith("Ollama unreachable at")
    assert detail.endswith("within 10s")
    # The web app's failure copy reads any "timeout" in the error as a stalled
    # local model; a host it cannot reach is not one.
    assert "timeout" not in detail.lower()
    messages = [r.getMessage() for r in caplog.records]
    failed = [m for m in messages if m.startswith("Ollama generate failed")]
    assert len(failed) == 1
    assert "exc=ConnectTimeout" in failed[0]
    assert f"stream={stream}" in failed[0]
    assert failed[0].endswith("timeout_s=10")
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_stream_read_timeout_is_logged_as_stream(monkeypatch, caplog):
    generate = _failing(monkeypatch, stream=True, exc=httpx.ReadTimeout(""))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await generate({"prompt": SENTINEL, "model": "m"})

    assert raised.value.status_code == 502
    assert "300s" in raised.value.detail
    assert "ReadTimeout" in raised.value.detail
    assert "Ollama generate timed out" in caplog.text
    assert "stream=True" in caplog.text
    assert "exc=ReadTimeout" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_stream_http_error_is_logged_without_the_body(monkeypatch, caplog):
    # The stream path reads the body right after its log call -- the one
    # place an edit could most easily interpolate it into the line.
    class _ErrorResponse:
        status_code = 500

        async def aread(self):
            return f"model runner crashed: {SENTINEL}".encode()

    async def enter():
        return _ErrorResponse()

    _patch_stream(monkeypatch, enter)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        with pytest.raises(HTTPException) as raised:
            await llm_openai._ollama_stream_generate({"prompt": SENTINEL, "model": "m"})

    assert raised.value.status_code == 502
    # The body goes back to the control plane in the reply, never to the log.
    assert raised.value.detail.startswith("Ollama error:")
    assert "Ollama generate error" in caplog.text
    assert "stream=True" in caplog.text
    assert "status=500" in caplog.text
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_success_lines_carry_the_budget_and_both_clocks(monkeypatch, caplog):
    captured: Dict[str, Any] = {}

    async def post(url, body):
        captured["body"] = body
        return _Resp(
            200,
            {"response": SENTINEL, "model": "m", "prompt_eval_count": 3, "eval_count": 2},
        )

    _patch_post(monkeypatch, post)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        out = await llm_openai._ollama_generate({"prompt": SENTINEL, "model": "m"})

    assert out["output"] == SENTINEL
    start = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Ollama generate:")]
    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Ollama generate complete")]
    assert len(start) == 1 and "timeout_s=300" in start[0]
    assert len(done) == 1 and "elapsed_s=" in done[0] and "wall_s=" in done[0]
    assert SENTINEL not in caplog.text
