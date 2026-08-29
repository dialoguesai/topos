"""P1 of PLAN_HOME_CHAT_STREAMING_SLA — the engine half of the stream protocol.

Guards, each pinned to a 2026-08-11 incident behavior:

- streaming think now defaults OFF (the old ``default=None`` let qwen3.5 burn
  the whole budget on chain-of-thought this loop then DISCARDED);
- thinking tokens are forwarded as typed frames, not dropped;
- an exhausted budget is a typed error, never an empty success;
- ``llm_cancel`` aborts an in-flight generation and the original request
  answers a typed 499 (the frame that used to just... never come);
- every frame carries the request id (the relay's single-pending fallback
  must never have to guess).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

import topos.core.handlers.device as device
from topos.services.llm import openai as llm_openai
from topos.services.llm.openai import LlmTypedError


class _FakeAdapter:
    """Capability probe stand-in: passes `desired` through untouched."""

    def resolve_think(self, model: str, desired):
        return desired


def _stream_client_factory(captured: Dict[str, Any], lines: List[dict]):
    """An httpx.AsyncClient stand-in whose .stream() yields ``lines``."""

    class _StreamResponse:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            for item in lines:
                yield json.dumps(item)

    class _StreamCM:
        async def __aenter__(self):
            return _StreamResponse()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):  # noqa: A002
            captured["url"] = url
            captured["body"] = json
            return _StreamCM()

    return _Client


def _patch_stream(monkeypatch, captured: Dict[str, Any], lines: List[dict]) -> None:
    monkeypatch.setattr(llm_openai.httpx, "AsyncClient", _stream_client_factory(captured, lines))
    monkeypatch.setattr(llm_openai, "_stream_ollama_adapter", lambda base: _FakeAdapter())
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_base_url", "http://localhost:11434")
    monkeypatch.setattr(llm_openai.settings, "engine_ollama_generate_timeout_sec", 300.0)
    monkeypatch.setattr(llm_openai.settings, "sanitization_ollama_default_model", "llama3.2")
    monkeypatch.setattr(llm_openai, "_ensure_ollama_running", lambda _base=None: None)


_DONE = {"done": True, "prompt_eval_count": 10, "eval_count": 5, "done_reason": "stop"}


@pytest.mark.asyncio
async def test_stream_think_defaults_off_and_keeps_the_model_warm(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_stream(monkeypatch, captured, [{"response": "hi", "model": "qwen3.5:9b-mlx"}, _DONE])

    out = await llm_openai._ollama_stream_generate(
        {"prompt": "q", "model": "qwen3.5:9b-mlx", "max_tokens": 1600}
    )
    assert out["output"] == "hi"
    assert captured["body"]["think"] is False
    assert captured["body"]["keep_alive"] == "30m"
    # Think-off: the budget is a cap, not a target — no floor applied.
    assert captured["body"]["options"]["num_predict"] == 1600


@pytest.mark.asyncio
async def test_stream_explicit_think_true_floors_the_budget(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_stream(
        monkeypatch,
        captured,
        [{"thinking": "hmm", "model": "m"}, {"response": "4", "model": "m"}, _DONE],
    )

    out = await llm_openai._ollama_stream_generate(
        {"prompt": "q", "model": "qwen3.5:9b-mlx", "max_tokens": 512, "think": True}
    )
    assert out["output"] == "4"
    assert captured["body"]["think"] is True
    # Thinking + answer share num_predict (live-verified): floored to survive.
    assert captured["body"]["options"]["num_predict"] == 2048
    assert out["thinking_chars"] == len("hmm")


@pytest.mark.asyncio
async def test_stream_forwards_thinking_as_typed_frames_not_discarded(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_stream(
        monkeypatch,
        captured,
        [
            {"thinking": "step 1. ", "model": "m"},
            {"thinking": "step 2.", "model": "m"},
            {"response": "answer", "model": "m"},
            _DONE,
        ],
    )
    deltas: List[str] = []
    frames: List[tuple] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    async def on_protocol(kind: str, fields: Dict[str, Any]) -> None:
        frames.append((kind, fields))

    out = await llm_openai._ollama_stream_generate(
        {"prompt": "q", "model": "m", "think": True},
        on_delta=on_delta,
        on_protocol=on_protocol,
    )
    assert deltas == ["answer"]
    thinking = [f for k, f in frames if k == "thinking"]
    assert [f["thinking_delta"] for f in thinking] == ["step 1. ", "step 2."]
    # Thinking never leaks into the visible answer.
    assert out["output"] == "answer"


@pytest.mark.asyncio
async def test_exhausted_budget_is_a_typed_error_not_an_empty_success(monkeypatch):
    captured: Dict[str, Any] = {}
    _patch_stream(
        monkeypatch,
        captured,
        [
            {"thinking": "reasoning forever...", "model": "m"},
            {"done": True, "prompt_eval_count": 10, "eval_count": 120, "done_reason": "length"},
        ],
    )
    with pytest.raises(LlmTypedError) as raised:
        await llm_openai._ollama_stream_generate({"prompt": "q", "model": "m", "think": True})
    assert raised.value.code == "thinking_budget_exhausted"
    assert "reasoning" in raised.value.message


@pytest.mark.asyncio
async def test_large_truncated_prompt_gets_a_soft_context_verdict(monkeypatch):
    captured: Dict[str, Any] = {}
    big_prompt = "x" * 40_000  # ~10k estimated tokens
    _patch_stream(
        monkeypatch,
        captured,
        [
            {"response": "confident answer", "model": "m"},
            {"done": True, "prompt_eval_count": 2000, "eval_count": 5, "done_reason": "stop"},
        ],
    )
    out = await llm_openai._ollama_stream_generate({"prompt": big_prompt, "model": "m"})
    # Soft flag, never an error: the answer may still be right, but the caller
    # can now say "older context was dropped" instead of nothing.
    assert out["output"] == "confident answer"
    assert out["context_truncated"]["prompt_eval_count"] == 2000
    assert out["context_truncated"]["estimated_prompt_tokens"] == 10_000


# ---------------------------------------------------------------------------
# handler-level: frames, ack, cancel
# ---------------------------------------------------------------------------


class _FakeCpClient:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_message(self, message: Dict[str, Any]) -> None:
        self.sent.append(message)


class _FakeLlm:
    def __init__(self, behavior) -> None:
        self._behavior = behavior

    async def generate(self, payload, on_delta=None, on_protocol=None):
        return await self._behavior(payload, on_delta, on_protocol)


def _install_llm(monkeypatch, cp_client, behavior) -> None:
    monkeypatch.setattr(device.engine_state, "control_plane_client", cp_client, raising=False)

    class _Services:
        llm = _FakeLlm(behavior)

    monkeypatch.setattr(device, "get_services", lambda: _Services())


@pytest.mark.asyncio
async def test_handler_emits_ack_first_and_every_frame_carries_the_id(monkeypatch):
    cp = _FakeCpClient()

    async def behavior(payload, on_delta, on_protocol):
        await on_protocol("thinking", {"thinking_delta": "hm"})
        await on_delta("hi")
        return {"output": "hi", "model": "m", "usage": {}}

    _install_llm(monkeypatch, cp, behavior)
    result = await device.handle_llm_generation(
        {"id": "req-1", "type": "llm_generation", "payload": {"prompt": "q", "stream": True}}
    )
    assert result == {"id": "req-1", "status": "ok", "payload": {"output": "hi", "model": "m", "usage": {}}}
    # Frame contract: interim traffic is status:"chunk" with delta always
    # present; kind frames carry seq; EVERY frame has the request id.
    assert all(m["id"] == "req-1" and m["status"] == "chunk" for m in cp.sent)
    assert cp.sent[0]["payload"]["kind"] == "ack"
    assert cp.sent[0]["payload"]["delta"] == ""
    kinds = [m["payload"].get("kind") for m in cp.sent]
    assert kinds == ["ack", "thinking", None]
    assert cp.sent[1]["payload"]["thinking_delta"] == "hm"
    assert cp.sent[2]["payload"]["delta"] == "hi"
    seqs = [m["payload"]["seq"] for m in cp.sent if "seq" in m["payload"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_handler_maps_typed_errors_to_string_error_codes(monkeypatch):
    cp = _FakeCpClient()

    async def behavior(payload, on_delta, on_protocol):
        raise LlmTypedError("thinking_budget_exhausted", "budget gone")

    _install_llm(monkeypatch, cp, behavior)
    result = await device.handle_llm_generation(
        {"id": "req-2", "type": "llm_generation", "payload": {"prompt": "q", "stream": True}}
    )
    assert result["status"] == "error"
    assert result["error_code"] == "thinking_budget_exhausted"
    assert result["error"] == "budget gone"


@pytest.mark.asyncio
async def test_llm_cancel_aborts_the_generation_and_answers_499(monkeypatch):
    cp = _FakeCpClient()
    started = asyncio.Event()

    async def behavior(payload, on_delta, on_protocol):
        started.set()
        await asyncio.sleep(60)  # cancelled long before this elapses
        return {"output": "never", "model": "m", "usage": {}}

    _install_llm(monkeypatch, cp, behavior)
    gen_task = asyncio.create_task(
        device.handle_llm_generation(
            {"id": "req-3", "type": "llm_generation", "payload": {"prompt": "q", "stream": True}}
        )
    )
    await started.wait()

    # The cancel frame carries its OWN id — reusing the target's id would let
    # this reply settle the original request's future on the control plane.
    reply = await device.handle_llm_cancel(
        {"id": "cancel-1", "type": "llm_cancel", "payload": {"target_request_id": "req-3"}}
    )
    assert reply == {"id": "cancel-1", "status": "ok", "payload": {"cancelled": True}}

    with pytest.raises(asyncio.CancelledError):
        await gen_task
    # The original request answered a typed 499 before dying, so the control
    # plane's pending future settles instead of waiting out its wall cap.
    terminal = [m for m in cp.sent if m.get("status") == "error"]
    assert len(terminal) == 1
    assert terminal[0]["id"] == "req-3"
    assert terminal[0]["error_code"] == 499
    # And the task registry does not leak the cancelled entry.
    assert "req-3" not in device._LLM_GENERATION_TASKS


@pytest.mark.asyncio
async def test_llm_cancel_for_unknown_request_reports_not_found(monkeypatch):
    reply = await device.handle_llm_cancel(
        {"id": "cancel-2", "type": "llm_cancel", "payload": {"target_request_id": "ghost"}}
    )
    assert reply == {
        "id": "cancel-2",
        "status": "ok",
        "payload": {"cancelled": False, "reason": "not_found"},
    }


@pytest.mark.asyncio
async def test_llm_cancel_requires_a_target(monkeypatch):
    reply = await device.handle_llm_cancel({"id": "cancel-3", "type": "llm_cancel", "payload": {}})
    assert reply["status"] == "error"
    assert reply["error_code"] == 422
