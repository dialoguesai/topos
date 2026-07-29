from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

from ...core import state
from ...openai_client import OpenAIError
from ...config.settings import settings

logger = logging.getLogger("topos.services.llm")


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


def _resolve_payload_think(payload: Dict[str, Any], *, default: Optional[bool]) -> Optional[bool]:
    """Honor an explicit payload.think; otherwise use ``default``.

    Non-stream llm_generation (routines / API) defaults to think=False so
    thinking models (qwen3.5, …) do not burn the whole num_predict budget on
    chain-of-thought and return an empty ``response``. Streaming chat keeps
    the model default (``default=None`` → omit the param).
    """
    if "think" in payload:
        raw = payload.get("think")
        if raw is None:
            return None
        return bool(raw)
    return default


async def _ollama_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    prompt = payload.get("prompt") or ""
    model_raw = payload.get("model")
    model = (
        (model_raw.strip() if isinstance(model_raw, str) else "")
        or settings.sanitization_ollama_default_model
    )
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature")
    base = settings.engine_ollama_base_url.rstrip("/")
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
) -> Dict[str, Any]:
    """Stream Ollama ``/api/generate`` and optionally emit deltas via ``on_delta``."""
    prompt = payload.get("prompt") or ""
    model_raw = payload.get("model")
    model = (
        (model_raw.strip() if isinstance(model_raw, str) else "")
        or settings.sanitization_ollama_default_model
    )
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature")
    base = settings.engine_ollama_base_url.rstrip("/")
    body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
    opts: Dict[str, Any] = {}
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if temperature is not None:
        opts["temperature"] = temperature
    if opts:
        body["options"] = opts
    # Streaming chat: leave thinking at the model default unless the client
    # opted in/out explicitly. Still use the longer generate timeout.
    think = _resolve_payload_think(payload, default=None)
    if think is not None:
        body["think"] = think
    timeout = _ollama_generate_timeout()
    output_parts: list[str] = []
    resp_model = model
    prompt_tokens = 0
    completion_tokens = 0
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
                    chunk = str(data.get("response") or "")
                    if chunk:
                        output_parts.append(chunk)
                        if on_delta is not None:
                            result = on_delta(chunk)
                            if hasattr(result, "__await__"):
                                await result
                    if data.get("done"):
                        prompt_tokens = int(data.get("prompt_eval_count") or prompt_tokens)
                        completion_tokens = int(data.get("eval_count") or completion_tokens)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama unreachable at {base}: {exc}",
        ) from exc

    text = "".join(output_parts).strip()
    total_tokens = prompt_tokens + completion_tokens
    usage: Dict[str, Any] = {}
    if total_tokens > 0:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    return {"output": text, "model": resp_model, "usage": usage}


async def _ollama_list_model_names() -> list[str]:
    base = settings.engine_ollama_base_url.rstrip("/")
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
    names = [str(m.get("name", "")).strip() for m in data.get("models", []) if m.get("name")]
    unique = sorted(set(names))
    logger.info("Ollama list models complete: count=%d", len(unique))
    return unique


class OpenAILLMService:
    async def list_ollama_models(self) -> Dict[str, Any]:
        if not settings.enable_llm or state.get_engine_mode() != "full":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="LLM is disabled")
        models = await _ollama_list_model_names()
        return {"models": models}

    async def generate(
        self,
        payload: Dict[str, Any],
        *,
        on_delta: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if not settings.enable_llm or state.get_engine_mode() != "full":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="LLM is disabled")

        provider = _normalize_provider(payload.get("provider"))
        if provider == "ollama":
            logger.info("LLM generate routed to Ollama (model=%r)", payload.get("model"))
            if payload.get("stream"):
                return await _ollama_stream_generate(payload, on_delta=on_delta)
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
