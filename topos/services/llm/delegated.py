"""Stateless delegated text chat. Only caller messages; no memory, tools, or fallback."""
from __future__ import annotations
import json
import asyncio
from urllib.parse import urlsplit
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi import HTTPException
from ...config.settings import settings
from ...core import state


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=262144)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=128)
    max_tokens: int = Field(ge=1, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def bounded(self):
        if not self.model.strip() or sum(len(m.content.encode()) for m in self.messages) > 262144:
            raise ValueError("Invalid model or input size")
        if not any(m.role == "user" for m in self.messages):
            raise ValueError("A user message is required")
        return self


def local_base():
    base = settings.engine_ollama_base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(503, "Delegated local models require a loopback Ollama endpoint")
    return base


def local_completion(details):
    return (isinstance(details, dict) and not details.get("remote_host") and not details.get("remote_model")
            and "completion" in (details.get("capabilities") or [])
            and bool(details.get("model_info")))


async def catalog():
    if not settings.enable_llm or state.get_engine_mode() != "full":
        raise HTTPException(403, "LLM is disabled")
    base = local_base()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(base + "/api/tags")
        response.raise_for_status()
        rows = response.json().get("models") or []
        semaphore = asyncio.Semaphore(4)
        async def inspect(row):
            name = row.get("name")
            if not isinstance(name, str) or row.get("remote_host") or row.get("remote_model"):
                return None
            async with semaphore:
                response = await client.post(base + "/api/show", json={"model": name})
                response.raise_for_status()
                return name if local_completion(response.json()) else None
        names = await asyncio.gather(*(inspect(row) for row in rows[:100]), return_exceptions=True)
    return {"models": sorted({name for name in names if isinstance(name, str)}), "reachable": True,
            "truncated": len(rows) > 100}


async def chat(payload, on_delta):
    body = ChatRequest.model_validate(payload)
    if not settings.enable_llm or state.get_engine_mode() != "full":
        raise HTTPException(403, "LLM is disabled")
    base = local_base()
    # Revalidate immediately before execution: an Ollama cloud alias must never satisfy local-only consent.
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(base + "/api/show", json={"model": body.model})
        response.raise_for_status()
        if not local_completion(response.json()):
            raise HTTPException(403, "Model is not a verified local text model")
    from ... import local_serving
    request = {"model": body.model, "messages": [m.model_dump() for m in body.messages],
               "stream": True, "options": {"num_predict": body.max_tokens}}
    if body.temperature is not None:
        request["options"]["temperature"] = body.temperature
    output = []
    usage = {}
    done = False
    # Shares the node's admission queue with its owner's work, at the outside caller's priority.
    async with local_serving.request_async(request, limit=290) as prepared:
        async with httpx.AsyncClient(timeout=httpx.Timeout(290, connect=10)) as client:
            async with client.stream("POST", base + "/api/chat",
                                     json=prepared) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise HTTPException(502, "Model execution failed")
                    delta = (event.get("message") or {}).get("content") or ""
                    if delta:
                        output.append(delta)
                        await on_delta(delta)
                    if event.get("done"):
                        done = True
                        usage = {"prompt_tokens": event.get("prompt_eval_count"),
                                 "completion_tokens": event.get("eval_count")}
    if not done:
        raise HTTPException(502, "Model response was interrupted")
    return {"output": "".join(output), "model": body.model, "usage": usage,
            "finish_reason": "length" if event.get("done_reason") == "length" else "stop"}
