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
        wallet_gated: bool = False,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "https://api.openai.com/v1").rstrip("/")
        self._default_model = str(default_model or "gpt-4o-mini").strip()
        self._unavailable_error = unavailable_error
        self._wallet_gated = bool(wallet_gated)

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = config or {}
        subtype = config.get("subtype") or ""
        model = str(config.get("model") or self._default_model).strip() or self._default_model
        if not self._api_key:
            return {"status": "deferred", "error": self._unavailable_error, "model": model}
        denied = self._wallet_deny(model)
        if denied is not None:
            return denied
        prompt = build_generative_prompt(subtype, payload)
        try:
            completed = self._chat_completion(model=model, prompt=prompt)
            response_text = str(completed.get("text") or "")
            out = parse_generative_response(response_text, subtype, model, payload=payload)
            out["model"] = model
            usage = completed.get("usage")
            if isinstance(usage, dict):
                out["usage"] = usage
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

    def _wallet_deny(self, model: str) -> Optional[Dict[str, Any]]:
        if not self._wallet_gated:
            return None
        try:
            from ..hosted_llm_wallet import INSUFFICIENT_CREDITS, hosted_llm_wallet_allows

            if hosted_llm_wallet_allows():
                return None
            return {"status": "deferred", "error": INSUFFICIENT_CREDITS, "model": model}
        except Exception:  # noqa: BLE001 — a probe failure must not block BYOK-style calls
            return None

    def _chat_completion(self, *, model: str, prompt: str) -> Dict[str, Any]:
        denied = self._wallet_deny(model)
        if denied is not None:
            raise RuntimeError(str(denied.get("error") or "insufficient_credits"))
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
        text = ""
        if choices:
            message = (choices[0] or {}).get("message") or {}
            text = str(message.get("content") or "").strip()
        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(raw_usage.get("prompt_tokens") or 0)
        completion_tokens = int(raw_usage.get("completion_tokens") or 0)
        total_tokens = int(raw_usage.get("total_tokens") or 0)
        if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens
        return {
            "text": text,
            "usage": {
                "prompt_tokens": max(0, prompt_tokens),
                "completion_tokens": max(0, completion_tokens),
                "total_tokens": max(0, total_tokens),
            },
        }
