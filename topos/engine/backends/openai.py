"""OpenAI platform / BYOK backend for generative enrichment."""

from __future__ import annotations

from .openai_compatible import OpenAICompatibleAdapter


class OpenAIAdapter(OpenAICompatibleAdapter):
    """Platform OpenAI or env-configured API key."""

    def __init__(self, *, wallet_gated: bool = False) -> None:
        try:
            from ...config.settings import settings

            api_key = str(settings.openai_api_key or "").strip()
            base_url = str(settings.openai_base_url or "https://api.openai.com/v1")
            default_model = str(settings.openai_model or "gpt-4o-mini")
        except Exception:
            api_key = ""
            base_url = "https://api.openai.com/v1"
            default_model = "gpt-4o-mini"
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            default_model=default_model,
            unavailable_error="openai_unreachable",
            wallet_gated=wallet_gated,
        )
