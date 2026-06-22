"""OpenAI-compatible chat completions backend for generative enrichment."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .generative_prompts import build_generative_prompt
from .generative_response import parse_generative_response

logger = logging.getLogger("topos.engine.openai_compatible")


class OpenAICompatibleAdapter:
    """Sync chat-completions adapter (OpenAI API shape)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        default_model: str = "gpt-4o-mini",
        unavailable_error: str = "llm_unreachable",
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "https://api.openai.com/v1").rstrip("/")
        self._default_model = str(default_model or "gpt-4o-mini").strip()
        self._unavailable_error = unavailable_error

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        subtype = config.get("subtype") or ""
        model = str(config.get("model") or self._default_model).strip() or self._default_model
        if not self._api_key:
            return {"status": "deferred", "error": self._unavailable_error, "model": model}
        prompt = build_generative_prompt(subtype, payload)
        try:
            response_text = self._chat_completion(model=model, prompt=prompt)
            out = parse_generative_response(response_text, subtype, model, payload=payload)
            out["model"] = model
            return out
        except urllib.error.URLError:
            return {"status": "deferred", "error": self._unavailable_error, "model": model}
        except RuntimeError as exc:
            if "request failed" in str(exc).lower():
                return {"status": "deferred", "error": self._unavailable_error, "model": model}
            return {"error": str(exc), "model": model}
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenAI-compatible inference failed: %s", exc)
            return {"error": str(exc), "model": model}

    def _chat_completion(self, *, model: str, prompt: str) -> str:
        url = f"{self._base_url}/chat/completions"
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": prompt},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"OpenAI-compatible request failed: {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI-compatible request failed: {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = (choices[0] or {}).get("message") or {}
        return str(message.get("content") or "").strip()
