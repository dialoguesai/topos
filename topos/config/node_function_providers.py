"""Shared provider vocabulary for per-node-function LLM configs.

Facts extraction and the conversation-context classifier accept the same
provider set as signal extraction: local Ollama, Topos-hosted OpenAI
("platform"), the owner's own OpenAI key ("openai"), and Topos Secure
("redpill"). Each function stores its override in its own engine_config keys;
this module only owns the vocabulary and the hosted-model defaults so the two
configs cannot drift apart.
"""

from __future__ import annotations

from typing import Any

NODE_FUNCTION_LLM_PROVIDERS: frozenset[str] = frozenset({"ollama", "platform", "openai", "redpill"})


def normalize_provider(value: Any) -> str:
    """Lowercased provider when valid, "" otherwise (unset ⇒ ollama/legacy)."""
    provider = str(value or "").strip().lower()
    return provider if provider in NODE_FUNCTION_LLM_PROVIDERS else ""


def hosted_default_model(settings: Any, provider: str) -> str:
    """The model a hosted provider runs when the override names none."""
    if provider == "redpill":
        from ..engine.backends.redpill import DEFAULT_REDPILL_MODEL

        return DEFAULT_REDPILL_MODEL
    return str(getattr(settings, "openai_model", "") or "gpt-4o-mini").strip()


def engine_provider_for(provider: str) -> str:
    """UI provider → engine backend provider.

    ``platform`` stays ``platform`` so Engine uses the wallet-gated Topos-hosted
    OpenAI adapter rather than the BYOK one. ``openai`` (the owner's key) is
    unbilled.
    """
    return provider
