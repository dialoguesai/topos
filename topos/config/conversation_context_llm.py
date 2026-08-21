"""Conversation-context classifier model: env default + per-node device override.

The owner picks which local model auto-categorizes new conversations as
work/personal (features.signal.conversation_context). Resolution order, first
non-empty wins — same contract as facts_llm:

  1. engine_config["conversation_context_llm_model"] — device override the UI
     writes; takes effect on the next enrichment batch, no restart needed.
  2. Settings.ollama_extraction_model — the ingest-time extraction tier.
  3. Settings.ollama_query_model — the floor tier.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .node_function_providers import (
    NODE_FUNCTION_LLM_PROVIDERS,
    hosted_default_model,
    normalize_provider,
)

if TYPE_CHECKING:
    from topos.config.model_packs import RoleBinding

logger = logging.getLogger("topos.config.conversation_context_llm")

ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL = "conversation_context_llm_model"
ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_PROVIDER = "conversation_context_llm_provider"

_MAX_MODEL_NAME_LEN = 200


def _read_engine_config_value(conn: Optional[sqlite3.Connection], key: str) -> Optional[str]:
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            return str(row["value"])  # sqlite3.Row
        except (TypeError, IndexError, KeyError):
            return str(row[0])
    except Exception:  # noqa: BLE001 — missing table/row on fresh DB is not fatal
        return None


def device_context_llm_model(conn: Optional[sqlite3.Connection]) -> str:
    raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL)
    return str(raw or "").strip()


def device_context_llm_provider(conn: Optional[sqlite3.Connection]) -> str:
    """The device provider override, or "" when unset (⇒ ollama/legacy)."""
    return normalize_provider(
        _read_engine_config_value(conn, ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_PROVIDER)
    )


def resolve_context_llm_model(settings: Any, conn: Optional[sqlite3.Connection] = None) -> str:
    """Device override → engine default. Deliberately NO pack rung.

    Until 2026-08-15 the pack's `classify` binding sat between these two. It is
    gone with the role: conversation-context tagging is an INGEST function — it
    runs when data arrives, not when the owner asks something — and its model is
    chosen under Settings → Models → Node functions (the device override below),
    not by the query-time pack. A query pack steering an ingest model was the
    exact confusion the query-only pack schema removes.
    """
    override = device_context_llm_model(conn)
    if override:
        return override
    for attr in ("ollama_extraction_model", "ollama_query_model"):
        value = str(getattr(settings, attr, "") or "").strip()
        if value:
            return value
    return ""


def resolve_context_llm_request(
    settings: Any, conn: Optional[sqlite3.Connection] = None
) -> Tuple[str, str]:
    """(provider, model) for the classifier — same contract as facts_llm.

    Provider defaults to ollama, where the model resolves through the
    historical settings chain. A hosted provider only ever comes from the
    device override, and resolves the override model or that provider's own
    default — never the Ollama chain.
    """
    provider = device_context_llm_provider(conn) or "ollama"
    if provider == "ollama":
        return "ollama", resolve_context_llm_model(settings, conn)
    override = device_context_llm_model(conn)
    return provider, override or hosted_default_model(settings, provider)


def resolve_context_llm_params(
    conn: Optional[sqlite3.Connection], model: str
) -> Optional["RoleBinding"]:
    """Always None since 2026-08-15: the pack `classify` rung is retired.

    Conversation-context tagging is an ingest function; its model is chosen by
    `resolve_context_llm_model` (device override → settings default), and a
    query-time pack must not steer its inference knobs sideways. Kept as a seam
    so callers keep one shape while per-function parameters grow their own home
    under Node functions.
    """
    return None


def _validate_model_name(model: Any) -> str:
    model = str(model or "").strip()
    if len(model) > _MAX_MODEL_NAME_LEN:
        raise ValueError(f"model name too long (max {_MAX_MODEL_NAME_LEN} chars)")
    if model and any(ch in model for ch in " \t\n\r"):
        raise ValueError("model name must not contain whitespace")
    return model


def normalize_put_model(payload: Any) -> str:
    """{"model": "<name>"} or bare string → stored value; empty clears."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        model = payload
    elif isinstance(payload, dict):
        model = payload.get("model") or ""
    else:
        raise ValueError("body must be a JSON object with a 'model' string")
    return _validate_model_name(model)


def normalize_put_config(payload: Any) -> Tuple[str, str]:
    """Validate a PUT body → (provider, model) to store — same contract as
    facts_llm.normalize_put_config: no/ollama provider stores "" (legacy),
    hosted providers require a model except platform, empty model clears."""
    if payload is None:
        return "", ""
    if isinstance(payload, str):
        return "", _validate_model_name(payload)
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object with a 'model' string")
    provider = str(payload.get("provider") or "").strip().lower()
    model = _validate_model_name(payload.get("model") or "")
    if not provider or provider == "ollama":
        return "", model
    if provider not in NODE_FUNCTION_LLM_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(sorted(NODE_FUNCTION_LLM_PROVIDERS))}")
    if provider != "platform" and not model:
        raise ValueError("model is required for this provider")
    return provider, model


def effective_config_for_api(settings: Any, conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """Resolved classifier model + local model catalog for the settings picker."""
    override = device_context_llm_model(conn)
    provider_override = device_context_llm_provider(conn)
    provider, effective = resolve_context_llm_request(settings, conn)
    if override or provider_override:
        source = "device_override"
    elif str(getattr(settings, "ollama_extraction_model", "") or "").strip():
        source = "extraction_model_default"
    else:
        source = "query_model_fallback"

    available = []
    try:
        from ..engine.backends.ollama import OllamaAdapter

        adapter = OllamaAdapter()
        for name in adapter.list_models():
            available.append(
                {
                    "name": name,
                    "supports_thinking": adapter.model_supports_thinking(name),
                }
            )
    except Exception as exc:  # noqa: BLE001 — catalog is a nicety, never a failure
        logger.debug("context_llm available-model probe failed: %s", exc)

    return {
        "model": effective,
        "provider": provider,
        "override": override or None,
        "override_provider": provider_override or None,
        "source": source,
        "available_models": available,
    }
