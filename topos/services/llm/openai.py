from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

from ...core import state
from ...openai_client import OpenAIError
from ...config.settings import settings

logger = logging.getLogger("topos.services.llm")


class LlmTypedError(Exception):
    """A generation failure with a machine-readable code the control plane and
    frontend can branch on (PLAN_HOME_CHAT_STREAMING_SLA §2 terminal codes).

    ``code`` today: ``thinking_budget_exhausted`` — the model consumed its
    whole ``num_predict`` on chain-of-thought and emitted zero visible answer,
    which used to be returned as an EMPTY SUCCESS (the 2026-08-11 stall's
    quietest failure mode).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: Chat generations keep the model warm so a follow-up question does not pay a
#: cold reload mid-conversation (critic finding #3 in the SLA plan). Ollama's
#: default is 5m — shorter than a human reading a long answer.
_STREAM_KEEP_ALIVE = "30m"

#: Estimated chars-per-token for the context-truncation heuristic. Deliberately
#: coarse: the verdict is a soft flag on the result payload, never an error on
#: its own (a false positive that killed a good answer would be worse than the
#: silent truncation it guards against).
_CHARS_PER_TOKEN_ESTIMATE = 4


def _stream_ollama_adapter(base_url: str):
    """The engine's capability-probing adapter, shared caches and all.

    Imported lazily: the backends module is heavier company than this service
    needs at import time, and the adapter's module-level caches
    (_THINKING_CAPABILITY_CACHE, _THINK_DISABLE_REJECTED) are what make a
    fresh instance cheap.
    """
    from ...engine.backends.ollama import OllamaAdapter

    return OllamaAdapter(base_url=base_url)


def _normalize_provider(raw: Any) -> str:
    if raw is None:
        return "openai"
    if isinstance(raw, str):
        v = raw.lower().strip()
        if v in ("openai", "ollama"):
            return v
    return "openai"


def _ollama_generate_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        float(getattr(settings, "engine_ollama_generate_timeout_sec", 300.0) or 300.0),
        connect=10.0,
    )


def _ensure_ollama_running(base_url: str) -> None:
    """Start a local Ollama if this request needs one and :11434 is down.

    Blocking (open + poll). Async callers run it via ``asyncio.to_thread``.
    """
    from ...engine.ollama_runtime import ensure_running

    ensure_running(base_url=base_url)


def _resolve_payload_think(payload: Dict[str, Any], *, default: Optional[bool]) -> Optional[bool]:
    """Honor an explicit payload.think; otherwise use ``default``.

    Non-stream llm_generation (routines / API) defaults to think=False so
    thinking models (qwen3.5, …) do not burn the whole num_predict budget on
    chain-of-thought and return an empty ``response``. Streaming chat keeps
    the model default (``default=None`` → omit the param).

    A null is "not stated", not "omit the param". `GenerationRequest.think`
    exists now, so every dumped request carries the key, and reading a
    present-but-null as an instruction would flip the non-stream default for
    every caller that never mentioned thinking at all.
    """
    raw = payload.get("think")
    return default if raw is None else bool(raw)


async def _ollama_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    prompt = payload.get("prompt") or ""
    model_raw = payload.get("model")
    model = (
        (model_raw.strip() if isinstance(model_raw, str) else "")
        or settings.sanitization_ollama_default_model
    )
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature")
    num_ctx = payload.get("num_ctx")
    base = settings.engine_ollama_base_url.rstrip("/")
    await asyncio.to_thread(_ensure_ollama_running, base)
    # Routines / non-stream API: suppress CoT so the visible answer is not
    # starved. Models that reject think=false still get a request without it
    # (Ollama returns 400); we retry once without the flag below.
    think = _resolve_payload_think(payload, default=False)
    body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if think is not None:
        body["think"] = think
    opts: Dict[str, Any] = {}
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if temperature is not None:
        opts["temperature"] = temperature
    if num_ctx is not None:
        opts["num_ctx"] = num_ctx
    if opts:
        body["options"] = opts
    timeout = _ollama_generate_timeout()
    logger.info(
        "Ollama generate: model=%r base=%s prompt_chars=%d max_tokens=%s temperature=%s think=%s",
        model,
        base,
        len(prompt),
        max_tokens,
        temperature,
        think,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/api/generate", json=body)
            if (
                r.status_code >= 400
                and think is False
                and "disabling thinking" in (r.text or "").lower()
            ):
                logger.info(
                    "Ollama model %s rejects think=false; retrying without think flag",
                    model,
                )
                body.pop("think", None)
                r = await client.post(f"{base}/api/generate", json=body)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama unreachable at {base}: {exc}",
        ) from exc
    if r.status_code >= 400:
        detail = (r.text or str(r.status_code))[:800]
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Ollama error: {detail}")
    data = r.json()
    text = (data.get("response") or "").strip()
    resp_model = str(data.get("model") or model)
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    total_tokens = prompt_tokens + completion_tokens
    logger.info(
        "Ollama generate complete: model=%r response_chars=%d prompt_tokens=%d completion_tokens=%d",
        resp_model,
        len(text),
        prompt_tokens,
        completion_tokens,
    )
    usage: Dict[str, Any] = {}
    if total_tokens > 0:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    return {"output": text, "model": resp_model, "usage": usage}


async def _ollama_stream_generate(
    payload: Dict[str, Any],
    *,
    on_delta: Optional[Any] = None,
    on_protocol: Optional[Any] = None,
) -> Dict[str, Any]:
    """Stream Ollama ``/api/generate``, emitting visible deltas via ``on_delta``
    and protocol frames (thinking / heartbeat) via ``on_protocol(kind, fields)``.

    P1 of PLAN_HOME_CHAT_STREAMING_SLA: thinking now defaults OFF like the
    non-stream path — the old ``default=None`` let reasoning models burn the
    whole budget on chain-of-thought this loop then DISCARDED (the 2026-08-11
    stall). When thinking is on (explicit opt-in), its tokens are forwarded as
    a first-class stream instead of dropped, and the budget is floored so the
    visible answer survives the reasoning spend.
    """
    prompt = payload.get("prompt") or ""
    model_raw = payload.get("model")
    model = (
        (model_raw.strip() if isinstance(model_raw, str) else "")
        or settings.sanitization_ollama_default_model
    )
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature")
    num_ctx = payload.get("num_ctx")
    base = settings.engine_ollama_base_url.rstrip("/")
    await asyncio.to_thread(_ensure_ollama_running, base)
    body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
    # Same default as the non-stream sibling, adapted through the capability
    # probe: models without the thinking capability (or that 400 on
    # think=false) get the param omitted rather than a doomed request.
    desired_think = _resolve_payload_think(payload, default=False)
    adapter = _stream_ollama_adapter(base)
    # The probe is blocking urllib (cached per base_url+model after the first
    # call) — keep it off the event loop.
    think = await asyncio.to_thread(adapter.resolve_think, model, desired_think)
    if think is not None:
        body["think"] = think
    if think is True and max_tokens is not None:
        from ...engine.backends.ollama import _THINKING_NUM_PREDICT_FLOOR

        # Thinking and answer share num_predict (live-verified 2026-08-11):
        # an unfloored budget starves the visible answer to zero.
        max_tokens = max(int(max_tokens), _THINKING_NUM_PREDICT_FLOOR)
    body["keep_alive"] = _STREAM_KEEP_ALIVE
    opts: Dict[str, Any] = {}
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if temperature is not None:
        opts["temperature"] = temperature
    if num_ctx is not None:
        opts["num_ctx"] = num_ctx
    if opts:
        body["options"] = opts
    timeout = _ollama_generate_timeout()
    output_parts: list[str] = []
    thinking_chars = 0
    resp_model = model
    prompt_tokens = 0
    completion_tokens = 0
    done_reason = ""
    # Liveness (plan §2): phase-aware heartbeats whenever nothing else flows —
    # 10s pre-first-token (load + prefill are silent), 30s mid-stream. Emitted
    # frames of any kind reset the clock.
    phase = "loading"
    last_frame_at = time.monotonic()

    async def _emit_protocol(kind: str, fields: Dict[str, Any]) -> None:
        nonlocal last_frame_at
        last_frame_at = time.monotonic()
        if on_protocol is None:
            return
        result = on_protocol(kind, fields)
        if hasattr(result, "__await__"):
            await result

    async def _heartbeats() -> None:
        while True:
            await asyncio.sleep(1.0)
            interval = 10.0 if phase == "loading" else 30.0
            if time.monotonic() - last_frame_at >= interval:
                await _emit_protocol("heartbeat", {"phase": phase})

    hb_task = asyncio.create_task(_heartbeats()) if on_protocol is not None else None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{base}/api/generate", json=body) as response:
                if response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:800]
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Ollama error: {detail}",
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    resp_model = str(data.get("model") or resp_model)
                    thinking_chunk = str(data.get("thinking") or "")
                    if thinking_chunk:
                        # Forwarded, not discarded: reasoning is the liveness
                        # signal during an otherwise-silent stretch, and the
                        # UI renders it as progress.
                        phase = "thinking"
                        thinking_chars += len(thinking_chunk)
                        await _emit_protocol("thinking", {"thinking_delta": thinking_chunk})
                    chunk = str(data.get("response") or "")
                    if chunk:
                        phase = "answer"
                        last_frame_at = time.monotonic()
                        output_parts.append(chunk)
                        if on_delta is not None:
                            result = on_delta(chunk)
                            if hasattr(result, "__await__"):
                                await result
                    if data.get("done"):
                        prompt_tokens = int(data.get("prompt_eval_count") or prompt_tokens)
                        completion_tokens = int(data.get("eval_count") or completion_tokens)
                        done_reason = str(data.get("done_reason") or "")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama unreachable at {base}: {exc}",
        ) from exc
    finally:
        if hb_task is not None:
            hb_task.cancel()

    text = "".join(output_parts).strip()
    if not text and done_reason == "length":
        # The whole budget went to reasoning (or the budget was simply too
        # small). Returning this as an empty SUCCESS is what poisoned the
        # facts lane and stalled home chat — it is a typed failure now.
        raise LlmTypedError(
            "thinking_budget_exhausted",
            f"Model {resp_model} spent its whole token budget"
            + (" on reasoning" if thinking_chars else "")
            + " and produced no visible answer. Retry with a larger max_tokens or thinking disabled.",
        )
    total_tokens = prompt_tokens + completion_tokens
    usage: Dict[str, Any] = {}
    if total_tokens > 0:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    result: Dict[str, Any] = {"output": text, "model": resp_model, "usage": usage}
    if thinking_chars:
        result["thinking_chars"] = thinking_chars
    if done_reason:
        result["done_reason"] = done_reason
    # Soft context-truncation verdict (SLA plan §3, critic #2): Ollama drops
    # the prompt HEAD on overflow and reports only the tokens it kept. A soft
    # flag, never an error — the answer may still be right, but the caller can
    # now say "older context was dropped" instead of nothing.
    estimated_prompt_tokens = max(1, len(prompt) // _CHARS_PER_TOKEN_ESTIMATE)
    if (
        prompt_tokens > 0
        and estimated_prompt_tokens > 2048
        and prompt_tokens < estimated_prompt_tokens // 2
    ):
        result["context_truncated"] = {
            "prompt_eval_count": prompt_tokens,
            "estimated_prompt_tokens": estimated_prompt_tokens,
        }
    return result


async def _ollama_list_models() -> Dict[str, Any]:
    """Installed tags plus size / capabilities / modified_at from /api/tags.

    Capabilities and modified_at ride along for Branch A preselect/chips
    (PLAN_LOCAL_MODEL_QUICKSTART). Uninstalled starters still take their CTA
    size from the curated table (D1) — Ollama has no size for a tag that is
    not on the machine.
    """
    base = settings.engine_ollama_base_url.rstrip("/")
    await asyncio.to_thread(_ensure_ollama_running, base)
    timeout = httpx.Timeout(settings.ollama_list_timeout_sec, connect=10.0)
    logger.info("Ollama list models: base=%s timeout_sec=%s", base, settings.ollama_list_timeout_sec)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{base}/api/tags")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama unreachable at {base}: {exc}",
        ) from exc
    if r.status_code >= 400:
        detail = (r.text or str(r.status_code))[:800]
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Ollama error: {detail}")
    data = r.json()
    sizes: Dict[str, int] = {}
    capabilities: Dict[str, list[str]] = {}
    modified_at: Dict[str, str] = {}
    names: list[str] = []
    for entry in data.get("models", []) or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        names.append(name)
        raw_size = entry.get("size")
        if isinstance(raw_size, int) and raw_size >= 0:
            sizes[name] = raw_size
        raw_caps = entry.get("capabilities")
        if isinstance(raw_caps, list):
            caps = [str(c).strip() for c in raw_caps if str(c or "").strip()]
            if caps:
                capabilities[name] = caps
        modified = entry.get("modified_at") or entry.get("modifiedAt")
        if isinstance(modified, str) and modified.strip():
            modified_at[name] = modified.strip()
    unique = sorted(set(names))
    logger.info("Ollama list models complete: count=%d", len(unique))
    return {
        "models": unique,
        "sizes": sizes,
        "capabilities": capabilities,
        "modified_at": modified_at,
    }


async def _ollama_list_model_names() -> list[str]:
    payload = await _ollama_list_models()
    return list(payload.get("models") or [])


class OpenAILLMService:
    async def list_ollama_models(self) -> Dict[str, Any]:
        if not settings.enable_llm or state.get_engine_mode() != "full":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="LLM is disabled")
        return await _ollama_list_models()

    async def generate(
        self,
        payload: Dict[str, Any],
        *,
        on_delta: Optional[Any] = None,
        on_protocol: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if not settings.enable_llm or state.get_engine_mode() != "full":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="LLM is disabled")

        provider = _normalize_provider(payload.get("provider"))
        if provider == "ollama":
            logger.info("LLM generate routed to Ollama (model=%r)", payload.get("model"))
            if payload.get("stream"):
                return await _ollama_stream_generate(payload, on_delta=on_delta, on_protocol=on_protocol)
            return await _ollama_generate(payload)

        if not settings.openai_api_key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OPENAI_API_KEY is not set")
        try:
            result = await state.openai_client.generate(
                prompt=payload.get("prompt", ""),
                max_tokens=payload.get("max_tokens"),
                temperature=payload.get("temperature"),
            )
            return {"output": result["output"], "model": settings.openai_model, "usage": result["usage"]}
        except OpenAIError as exc:
            detail = str(exc)
            if "rate_limited" in detail:
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="LLM rate limited") from exc
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="LLM upstream error") from exc
