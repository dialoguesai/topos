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
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.config.facts_llm")

ENGINE_CONFIG_KEY_FACTS_LLM_MODEL = "facts_llm_model"

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


def resolve_facts_llm_model(settings: Any, conn: Optional[sqlite3.Connection] = None) -> str:
    """Effective model for the LLM fact pass ("" ⇒ pass stays inert)."""
    override = device_facts_llm_model(conn)

    # Resolution order (PLAN_MODEL_PACKS.md M3 / S6): device override → pack
    # `classify` role → this function's own default chain. Routed through the
    # one node resolver so precedence cannot drift from home chat / routines.
    # This module only ever runs against local Ollama — a cloud-bound pack
    # role falls through to the engine default rather than leaving the machine.
    engine_default = ""
    for attr in ("facts_llm_model", "ollama_extraction_model", "ollama_query_model"):
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

        # A pack can bind `classify` to a tag this machine never pulled; without
        # the live list the resolver trusts it and the Ollama call 404s mid-batch
        # (PLAN_LOCAL_MODEL_QUICKSTART §1.4).
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
    model = str(model).strip()
    if len(model) > _MAX_MODEL_NAME_LEN:
        raise ValueError(f"model name too long (max {_MAX_MODEL_NAME_LEN} chars)")
    if model and any(ch in model for ch in " \t\n\r"):
        raise ValueError("model name must not contain whitespace")
    return model


def effective_config_for_api(settings: Any, conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """Resolved facts-LLM model config + the local model catalog for a picker.

    ``available_models`` is best-effort: [] when Ollama is unreachable. Each
    entry carries ``supports_thinking`` so the UI can badge reasoning models —
    informational only; the adapter adapts automatically either way.
    """
    override = device_facts_llm_model(conn)
    effective = resolve_facts_llm_model(settings, conn)
    if override:
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
        "source": source,
        "device_override": override,
        "env_default": str(getattr(settings, "facts_llm_model", "") or "").strip(),
        "extraction_model": str(getattr(settings, "ollama_extraction_model", "") or "").strip(),
        "query_model": str(getattr(settings, "ollama_query_model", "") or "").strip(),
        "available_models": available,
    }
