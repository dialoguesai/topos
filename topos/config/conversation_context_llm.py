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
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from topos.config.model_packs import RoleBinding

logger = logging.getLogger("topos.config.conversation_context_llm")

ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL = "conversation_context_llm_model"

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


def resolve_context_llm_model(settings: Any, conn: Optional[sqlite3.Connection] = None) -> str:
    override = device_context_llm_model(conn)

    # Resolution order (PLAN_MODEL_PACKS.md M3 / S6): device override → pack
    # `classify` role → engine default, via the one node resolver.
    engine_default = ""
    for attr in ("ollama_extraction_model", "ollama_query_model"):
        value = str(getattr(settings, attr, "") or "").strip()
        if value:
            engine_default = value
            break

    if conn is not None:
        from .model_packs import (
            SOURCE_OVERRIDE,
            SOURCE_PACK,
            active_pack_dict,
            installed_local_models,
            resolve_model,
        )

        # See facts_llm: a pack tag the machine never pulled must fall to the
        # engine default here rather than 404 on first use (§1.4).
        installed = installed_local_models()

        resolved = resolve_model(
            role="classify",
            override=override or None,
            pack=active_pack_dict(conn),
            engine_default=(
                {"provider": "ollama", "model": engine_default} if engine_default else None
            ),
            installed_local_models=installed,
        )
        if resolved.source == SOURCE_OVERRIDE and resolved.model:
            return resolved.model
        if resolved.source == SOURCE_PACK and resolved.provider != "ollama":
            resolved = resolve_model(
                role="classify",
                engine_default=(
                    {"provider": "ollama", "model": engine_default} if engine_default else None
                ),
                installed_local_models=installed,
            )
        if resolved.model:
            return resolved.model

    return override or engine_default


def resolve_context_llm_params(
    conn: Optional[sqlite3.Connection], model: str
) -> Optional["RoleBinding"]:
    """The pack's `classify` binding, but only for the model it was set on.

    Picking the model and picking its parameters are one decision, so they live
    together: `resolve_context_llm_model` can return a device override or a
    settings default instead of the pack's model, and applying another
    binding's `thinking`/`context` to that model would run it at settings its
    owner never chose for it.
    """
    if conn is None or not model:
        return None

    from .model_packs import resolve_role_binding

    binding = resolve_role_binding(conn, "classify")
    if binding is None or binding.provider != "ollama" or binding.model != model:
        return None
    return binding


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
    model = str(model).strip()
    if len(model) > _MAX_MODEL_NAME_LEN:
        raise ValueError(f"model name too long (max {_MAX_MODEL_NAME_LEN} chars)")
    if model and any(ch in model for ch in " \t\n\r"):
        raise ValueError("model name must not contain whitespace")
    return model


def effective_config_for_api(settings: Any, conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """Resolved classifier model + local model catalog for the settings picker."""
    override = device_context_llm_model(conn)
    effective = resolve_context_llm_model(settings, conn)
    if override:
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
        "override": override or None,
        "source": source,
        "available_models": available,
    }
