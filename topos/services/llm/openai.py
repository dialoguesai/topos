from __future__ import annotations

import logging
from typing import Any, Dict

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
    body: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    opts: Dict[str, Any] = {}
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if temperature is not None:
        opts["temperature"] = temperature
    if opts:
        body["options"] = opts
    timeout = httpx.Timeout(settings.sanitization_ollama_timeout_sec, connect=10.0)
    logger.info(
        "Ollama generate: model=%r base=%s prompt_chars=%d max_tokens=%s temperature=%s",
        model,
        base,
        len(prompt),
        max_tokens,
        temperature,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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
    logger.info(
        "Ollama generate complete: model=%r response_chars=%d",
        resp_model,
        len(text),
    )
    return {"output": text, "model": resp_model, "usage": {}}


async def _ollama_list_model_names() -> list[str]:
    base = settings.engine_ollama_base_url.rstrip("/")
    timeout = httpx.Timeout(settings.sanitization_ollama_timeout_sec, connect=10.0)
    logger.info("Ollama list models: base=%s", base)
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

    async def generate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not settings.enable_llm or state.get_engine_mode() != "full":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="LLM is disabled")

        provider = _normalize_provider(payload.get("provider"))
        if provider == "ollama":
            logger.info("LLM generate routed to Ollama (model=%r)", payload.get("model"))
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
