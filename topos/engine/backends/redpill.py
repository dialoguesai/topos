"""Red Pill (Topos Secure) backend for generative enrichment."""

from __future__ import annotations

from typing import Optional

from .openai_compatible import OpenAICompatibleAdapter

DEFAULT_REDPILL_MODEL = "openai/gpt-oss-20b"
SUPPORTED_REDPILL_MODELS = frozenset(
    {
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b",
        "meta-llama/llama-3.3-70b-instruct",
    }
)


def resolve_redpill_model(model_override: Optional[str]) -> str:
    chosen = str(model_override or "").strip() or DEFAULT_REDPILL_MODEL
    if chosen not in SUPPORTED_REDPILL_MODELS:
        return DEFAULT_REDPILL_MODEL
    return chosen


class RedpillAdapter(OpenAICompatibleAdapter):
    """Topos Secure hosted models via Red Pill API."""

    def __init__(self) -> None:
        try:
            from ...config.settings import settings

            api_key = str(getattr(settings, "red_pill_api_key", None) or "").strip()
            base_url = str(getattr(settings, "red_pill_api_base", None) or "https://api.redpill.ai/v1")
            default_model = DEFAULT_REDPILL_MODEL
        except Exception:
            api_key = ""
            base_url = "https://api.redpill.ai/v1"
            default_model = DEFAULT_REDPILL_MODEL
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            default_model=default_model,
            unavailable_error="redpill_unreachable",
            wallet_gated=True,
        )
