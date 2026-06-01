from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .config.settings import settings

logger = logging.getLogger(__name__)


class OpenAIError(Exception):
    """Wrapper for upstream OpenAI errors."""


class OpenAIClient:
    """Minimal OpenAI chat completions client."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url
        self.timeout = settings.openai_timeout_seconds

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int],
        temperature: Optional[float],
    ) -> Dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: Dict[str, Any] = {
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": prompt},
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("OpenAI request failed: %s", exc)
            raise OpenAIError(f"request failed: {exc}") from exc

        if resp.status_code == 429:
            raise OpenAIError("rate_limited")

        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise OpenAIError(f"upstream_error: {resp.status_code}: {detail}")

        data = resp.json()
        try:
            message = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except Exception as exc:  # noqa: BLE001
            raise OpenAIError("invalid_response") from exc

        return {"output": message, "usage": usage}
