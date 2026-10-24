"""The model verify_claim runs on: the owner's pick, persisted beside the node's other per-function model settings.

verify_claim v2 (``query/verify_v2.py``) answers the Fun Camera's truth mirror, the owner door ``verify_claim_owner``
and the inbound truth lane with two backends: Topos Secure through the node's Red Pill key (``HostedChat``) and the
local Ollama server (``LocalChat``). Which one, and which model, used to be the environment alone
(``TOPOS_VERIFY_V2_MODEL`` = ``hosted`` | ``local``, ``TOPOS_VERIFY_V2_LOCAL_MODEL``). The owner now picks it in the
web app's Settings → General, like the facts and conversation-context models, and the pick is stored the same way:
two ``engine_config`` keys, read per call, so a change takes effect on the next check with no restart.

Resolution order, first that applies wins:

  1. ``engine_config["verify_claim_provider"]`` + ``["verify_claim_model"]``  — the pick the web app writes
     (``PUT /v1/verify-claim-model-config``); provider ``redpill`` names a hosted model, ``""`` (legacy shape, as the
     facts setting stores it) names a local Ollama model.
  2. ``TOPOS_VERIFY_V2_MODEL``                                                   — ``local`` or ``hosted`` with
     that side's default model (``TOPOS_VERIFY_V2_LOCAL_MODEL`` for local).
  3. hosted when a Red Pill key is configured, else local                        — as before the setting existed.

The inbound lane's own ``truth_inbound.model`` (``local`` | ``hosted``) is a choice of SIDE, not of model: it resolves
through the same pick (``hosted_model_for_lane`` / ``local_model_for_lane``), so a hosted pick is what the lane's
``hosted`` means and a local pick is what its ``local`` means, each falling back to today's default when the pick is
of the other kind. One model list for the feature, never two.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("topos.config.verify_claim_model")

ENGINE_CONFIG_KEY_VERIFY_CLAIM_MODEL = "verify_claim_model"
ENGINE_CONFIG_KEY_VERIFY_CLAIM_PROVIDER = "verify_claim_provider"

#: The two backends verify_claim has. ``ollama`` is stored as ``""`` (the legacy single-model shape every other picker
#: stores for a local model), so a node that predates the provider key reads the same row.
VERIFY_CLAIM_PROVIDERS: frozenset = frozenset({"ollama", "redpill"})
HOSTED_PROVIDER = "redpill"
LOCAL_PROVIDER = "ollama"

ENV_CHOICE = "TOPOS_VERIFY_V2_MODEL"
ENV_LOCAL_MODEL = "TOPOS_VERIFY_V2_LOCAL_MODEL"

_MAX_MODEL_NAME_LEN = 200

#: The pick as stored: ``(provider, model)`` with provider ``""`` (local) or ``"redpill"``; ``("", "")`` when unset.
Pick = Tuple[str, str]
NO_PICK: Pick = ("", "")


def _defaults():
    """The verify module's own defaults, imported late: it imports this module, and the constants are its."""
    from ..query import verify_v2_models as m

    return m.DEFAULT_HOSTED_MODEL, m.DEFAULT_LOCAL_MODEL, frozenset(m.VERIFY_HOSTED_MODELS)


def hosted_models_accepted() -> frozenset:
    """Hosted models this node will run verify_claim on: the ones verify names directly plus the Topos Secure set the
    enrichment adapter routes. A pick outside it is refused at the write, never silently replaced by another model."""
    from ..engine.backends.redpill import SUPPORTED_REDPILL_MODELS

    return frozenset(_defaults()[2]) | frozenset(SUPPORTED_REDPILL_MODELS)


def default_local_model() -> str:
    return (os.environ.get(ENV_LOCAL_MODEL) or "").strip() or _defaults()[1]


def default_hosted_model() -> str:
    return _defaults()[0]


# --- the stored pick -------------------------------------------------------------------------------------------------
def _read_engine_config_value(conn: Optional[sqlite3.Connection], key: str) -> Optional[str]:
    """Read engine_config without importing topos.core.state (avoids circular imports); None on a fresh database."""
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


def normalize_provider(value: Any) -> str:
    """``"redpill"`` for a hosted pick, ``""`` for a local one or anything unknown (unset ⇒ local, the legacy shape)."""
    provider = str(value or "").strip().lower()
    return HOSTED_PROVIDER if provider == HOSTED_PROVIDER else ""


def device_pick(conn: Optional[sqlite3.Connection]) -> Pick:
    """The owner's pick, or ``("", "")`` when none is stored. A provider with no model is no pick: the write refuses
    that shape, and a half-row from an older write must not choose a backend by itself."""
    model = str(_read_engine_config_value(conn, ENGINE_CONFIG_KEY_VERIFY_CLAIM_MODEL) or "").strip()
    if not model:
        return NO_PICK
    return normalize_provider(_read_engine_config_value(conn, ENGINE_CONFIG_KEY_VERIFY_CLAIM_PROVIDER)), model


def picked_side(pick: Optional[Pick]) -> str:
    """``"hosted"``, ``"local"`` or ``""`` for no pick."""
    provider, model = pick or NO_PICK
    if not model:
        return ""
    return "hosted" if provider == HOSTED_PROVIDER else "local"


# --- resolution ------------------------------------------------------------------------------------------------------
def hosted_model_for_lane(pick: Optional[Pick]) -> str:
    """What ``hosted`` means for verify_claim: the picked hosted model, else the default hosted model."""
    return pick[1] if picked_side(pick) == "hosted" else default_hosted_model()


def local_model_for_lane(pick: Optional[Pick]) -> str:
    """What ``local`` means for verify_claim: the picked local model, else ``TOPOS_VERIFY_V2_LOCAL_MODEL`` or the
    default local model."""
    return pick[1] if picked_side(pick) == "local" else default_local_model()


def _hosted_key_configured(settings: Any) -> bool:
    try:
        return bool(str(getattr(settings, "red_pill_api_key", None) or "").strip())
    except Exception:  # noqa: BLE001
        return False


def resolve_verify_claim_request(settings: Any, pick: Optional[Pick]) -> Tuple[str, str, str]:
    """``(provider, model, source)`` for the owner's verify_claim calls: ``provider`` is ``redpill`` or ``ollama``,
    ``source`` says which rung answered (``device_override``, ``env``, ``default``)."""
    side = picked_side(pick)
    if side == "hosted":
        return HOSTED_PROVIDER, pick[1], "device_override"
    if side == "local":
        return LOCAL_PROVIDER, pick[1], "device_override"
    choice = (os.environ.get(ENV_CHOICE) or "").strip().lower()
    if choice == "local":
        return LOCAL_PROVIDER, default_local_model(), "env"
    if choice == "hosted":
        return HOSTED_PROVIDER, default_hosted_model(), "env"
    if _hosted_key_configured(settings):
        return HOSTED_PROVIDER, default_hosted_model(), "default"
    return LOCAL_PROVIDER, default_local_model(), "default"


# --- the write -------------------------------------------------------------------------------------------------------
def _validate_model_name(model: Any) -> str:
    model = str(model or "").strip()
    if len(model) > _MAX_MODEL_NAME_LEN:
        raise ValueError(f"model name too long (max {_MAX_MODEL_NAME_LEN} chars)")
    if model and any(ch in model for ch in " \t\n\r"):
        raise ValueError("model name must not contain whitespace")
    return model


def normalize_put_config(payload: Any) -> Pick:
    """Validate a PUT body → ``(provider, model)`` to store.

    ``{"provider": "redpill", "model": <hosted id>}`` is a hosted pick and the model must be one this node runs;
    ``{"model": <ollama tag>}`` or ``{"provider": "ollama", ...}`` is a local pick and stores provider ``""`` (the
    legacy shape); ``{"model": ""}``, ``None`` or a bare empty string clears the pick. A bare string is a local
    model, as the other pickers read it. Raises ValueError on anything else so the API can 400 it.
    """
    if payload is None:
        return NO_PICK
    if isinstance(payload, str):
        return "", _validate_model_name(payload)
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object with a 'model' string")
    provider = str(payload.get("provider") or "").strip().lower()
    model = _validate_model_name(payload.get("model") or "")
    if not provider or provider == LOCAL_PROVIDER:
        return "", model
    if provider not in VERIFY_CLAIM_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(sorted(VERIFY_CLAIM_PROVIDERS))}")
    if not model:
        return NO_PICK  # a hosted provider with no model is a clear, as {"model": ""} is
    if model not in hosted_models_accepted():
        raise ValueError(f"hosted model not available on this node: {model}")
    return HOSTED_PROVIDER, model


def save_pick(conn: sqlite3.Connection, pick: Pick) -> None:
    from ..core.state import set_engine_config_value

    provider, model = pick
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_VERIFY_CLAIM_MODEL, model)
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_VERIFY_CLAIM_PROVIDER, provider)


# --- the read the picker shows ---------------------------------------------------------------------------------------
def effective_config_for_api(settings: Any, conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """The resolved verify_claim model, how it was chosen, and the catalogs a picker lists.

    ``available_models`` is the local Ollama catalog, best-effort ([] when Ollama is unreachable), the same probe
    the facts picker answers with; ``hosted_models`` is the hosted set this node accepts. The web app lists hosted
    models from the control plane's routed catalog, as the other pickers do; ``hosted_models`` is what a PUT will
    take, so a mismatch is visible rather than silent.
    """
    pick = device_pick(conn)
    provider, model, source = resolve_verify_claim_request(settings, pick)
    available = []
    try:
        from ..engine.backends.ollama import OllamaAdapter

        adapter = OllamaAdapter()
        for name in adapter.list_models():
            available.append({"name": name, "supports_thinking": adapter.model_supports_thinking(name)})
    except Exception as exc:  # noqa: BLE001 — catalog is a nicety, never a failure
        logger.debug("verify_claim available-model probe failed: %s", exc)
    return {
        "model": model,
        "provider": provider,
        "source": source,
        "device_override": pick[1],
        "device_override_provider": pick[0],
        "env_choice": (os.environ.get(ENV_CHOICE) or "").strip().lower(),
        "hosted_default": default_hosted_model(),
        "local_default": default_local_model(),
        "hosted_model": hosted_model_for_lane(pick),
        "local_model": local_model_for_lane(pick),
        "hosted_models": sorted(hosted_models_accepted()),
        "available_models": available,
    }
