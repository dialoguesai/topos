"""Facts-LLM extraction model: env default + per-node device override.

The owner picks which local model runs the role-gated LLM fact pass
(features.facts.llm_extract). Resolution order, first non-empty wins:

  1. engine_config["facts_llm_model"]      — device override the UI writes;
                                             takes effect on the next batch,
                                             no restart needed.
  2. Settings.facts_llm_model              — env TOPOS_FACTS_LLM_MODEL.
  3. Settings.ollama_extraction_model      — the ingest-time extraction tier.
  4. Settings.ollama_query_model           — the floor tier.

Thinking vs non-thinking is NOT configured here on purpose: the Ollama
adapter probes each model's capabilities (/api/show) and adapts the
``think`` flag automatically, so any model the user picks just works.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional, Tuple

from .node_function_providers import (
    NODE_FUNCTION_LLM_PROVIDERS,
    hosted_default_model,
    normalize_provider,
)

logger = logging.getLogger("topos.config.facts_llm")

ENGINE_CONFIG_KEY_FACTS_LLM_MODEL = "facts_llm_model"
ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER = "facts_llm_provider"

_MAX_MODEL_NAME_LEN = 200


def _read_engine_config_value(conn: Optional[sqlite3.Connection], key: str) -> Optional[str]:
    """Read engine_config without importing topos.core.state (avoids circular imports)."""
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


def device_facts_llm_model(conn: Optional[sqlite3.Connection]) -> str:
    """The device override, or "" when unset/cleared."""
    raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL)
    return str(raw or "").strip()


def device_facts_llm_provider(conn: Optional[sqlite3.Connection]) -> str:
    """The device provider override, or "" when unset (⇒ ollama/legacy)."""
    return normalize_provider(_read_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_PROVIDER))


def resolve_facts_llm_model(settings: Any, conn: Optional[sqlite3.Connection] = None) -> str:
    """Effective model for the LLM fact pass ("" ⇒ pass stays inert).

    Device override → this function's own default chain. Deliberately NO pack
    rung since 2026-08-15: facts extraction is an INGEST function — it runs when
    data arrives, not when the owner asks something — and its model is chosen
    under Settings → Models → Node functions (the device override below). The
    retired `classify` pack role used to sit between these rungs, which let a
    query-time pack steer an ingest model sideways; that is the exact confusion
    the query-only pack schema removes. This module only ever runs against local
    Ollama either way.
    """
    override = device_facts_llm_model(conn)
    if override:
        return override
    for attr in ("facts_llm_model", "ollama_extraction_model", "ollama_query_model"):
        value = str(getattr(settings, attr, "") or "").strip()
        if value:
            return value
    return ""


def resolve_facts_llm_request(
    settings: Any, conn: Optional[sqlite3.Connection] = None
) -> Tuple[str, str]:
    """(provider, model) for the LLM fact pass.

    Provider defaults to ollama (the only pre-provider behavior), where the
    model resolves through the historical env chain. A hosted provider only
    ever comes from the device override, and resolves the override model or
    that provider's own default — never the Ollama env chain.
    """
    provider = device_facts_llm_provider(conn) or "ollama"
    if provider == "ollama":
        return "ollama", resolve_facts_llm_model(settings, conn)
    override = device_facts_llm_model(conn)
    return provider, override or hosted_default_model(settings, provider)


def _validate_model_name(model: Any) -> str:
    model = str(model or "").strip()
    if len(model) > _MAX_MODEL_NAME_LEN:
        raise ValueError(f"model name too long (max {_MAX_MODEL_NAME_LEN} chars)")
    if model and any(ch in model for ch in " \t\n\r"):
        raise ValueError("model name must not contain whitespace")
    return model


def normalize_put_model(payload: Any) -> str:
    """Validate a PUT body ({"model": "<name>"} or a bare string) → stored value.

    Empty/None clears the override (resolution falls back to env defaults).
    Raises ValueError on garbage so the API can 400 it.
    """
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
    """Validate a PUT body → (provider, model) to store.

    Back-compat: a bare string or {"model": ...} with no provider is the
    legacy ollama-model write and stores provider "". {"model": ""} (or None)
    clears both. Hosted providers require a model, except platform whose model
    is Topos-chosen. Raises ValueError on garbage so the API can 400 it.
    """
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
    """Resolved facts-LLM model config + the local model catalog for a picker.

    ``available_models`` is best-effort: [] when Ollama is unreachable. Each
    entry carries ``supports_thinking`` so the UI can badge reasoning models —
    informational only; the adapter adapts automatically either way.
    """
    override = device_facts_llm_model(conn)
    provider_override = device_facts_llm_provider(conn)
    provider, effective = resolve_facts_llm_request(settings, conn)
    if override or provider_override:
        source = "device_override"
    elif str(getattr(settings, "facts_llm_model", "") or "").strip():
        source = "env"
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
        logger.debug("facts_llm available-model probe failed: %s", exc)

    return {
        "model": effective,
        "provider": provider,
        "source": source,
        "device_override": override,
        "device_override_provider": provider_override,
        "env_default": str(getattr(settings, "facts_llm_model", "") or "").strip(),
        "extraction_model": str(getattr(settings, "ollama_extraction_model", "") or "").strip(),
        "query_model": str(getattr(settings, "ollama_query_model", "") or "").strip(),
        "available_models": available,
    }
