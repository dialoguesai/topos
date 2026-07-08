"""Device-aware model recommendations for signal extraction.

Signals are derived by enrichment jobs bound to concrete models: small
HuggingFace task models (embeddings, entities, emotions — fine on any
supported device) and an Ollama LLM for topics, briefs, and goal extraction.
This module compares the effective Ollama model against what the device's
RAM can actually run, so the UI can suggest a minimum/recommended model
instead of silently letting enrichment thrash or defer.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, Optional

# (ram_gb upper bound exclusive, minimum model, recommended model,
#  runnable cap in billions of params, recommended cap in billions)
_RAM_BANDS = (
    (8.0, "llama3.2:1b", "llama3.2:3b", 4.0, 3.5),
    (16.0, "llama3.2:3b", "llama3.2", 8.5, 4.0),
    (float("inf"), "llama3.2", "llama3.2", 70.0, 70.0),
)

_REMOTE_PROVIDERS = frozenset({"platform", "openai", "redpill"})

_PARAM_TAG = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def estimate_params_b(model_name: str) -> float:
    """Billions of parameters parsed from the model tag ("llama3.2:3b" -> 3.0).

    Untagged names default to 3.0 — the size of the common `latest` tags we
    ship as defaults (llama3.2 == 3B).
    """
    name = str(model_name or "").strip().lower()
    if not name:
        return 3.0
    tag = name.split(":", 1)[-1]
    match = _PARAM_TAG.search(tag) or _PARAM_TAG.search(name)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return 3.0


def hf_job_models() -> Dict[str, str]:
    """HuggingFace signal-job models (small task models, any-device tier)."""
    from ...enrichment.models.mvp_defaults import MVP_JOB_SPECS

    return {
        job_id: model_path
        for job_id, _task, provider, model_path, _pref in MVP_JOB_SPECS
        if provider == "huggingface" and model_path
    }


def device_ram_gb() -> Optional[float]:
    try:
        from ...core.state import get_system_info

        ram_bytes = get_system_info().get("memory_total_bytes")
        if ram_bytes:
            return round(float(ram_bytes) / (1024**3), 1)
    except Exception:
        pass
    return None


def signal_model_recommendation(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Effective signal-extraction model judged against device capacity.

    Returns tier "recommended" | "minimum" | "stress": recommended fits the
    device comfortably, minimum is runnable but heavier than advised, stress
    exceeds what the RAM band can run.
    """
    from ...config.settings import settings
    from ...config.signal_extraction import resolve_signal_extraction_config

    cfg = resolve_signal_extraction_config(settings, conn)
    provider = str(cfg.provider or "ollama").strip().lower()
    effective_model = str(cfg.query_model or "").strip()
    ram_gb = device_ram_gb()

    base: Dict[str, Any] = {
        "provider": provider,
        "effective_model": effective_model,
        "ram_gb": ram_gb,
        "hf_jobs": hf_job_models(),
    }

    if provider in _REMOTE_PROVIDERS:
        return {
            **base,
            "tier": "recommended",
            "minimum_model": None,
            "ollama_query_model": None,
            "meets_minimum": True,
            "meets_recommended": True,
            "reason": f"{provider} runs remotely — no device model constraint",
        }

    if ram_gb is None:
        return {
            **base,
            "tier": "recommended",
            "minimum_model": None,
            "ollama_query_model": None,
            "meets_minimum": True,
            "meets_recommended": True,
            "reason": "device memory unknown — no recommendation applied",
        }

    minimum_model, recommended_model, cap_b, recommended_cap_b = next(
        band[1:] for band in _RAM_BANDS if ram_gb < band[0]
    )
    est_b = estimate_params_b(effective_model)
    meets_minimum = est_b <= cap_b
    meets_recommended = est_b <= recommended_cap_b
    if meets_recommended:
        tier = "recommended"
        reason = f"{effective_model or recommended_model} fits {ram_gb:g}GB RAM"
    elif meets_minimum:
        tier = "minimum"
        reason = (
            f"{effective_model} is heavy for {ram_gb:g}GB RAM — "
            f"try {recommended_model}"
        )
    else:
        tier = "stress"
        reason = (
            f"{effective_model} likely exceeds {ram_gb:g}GB RAM — "
            f"use {recommended_model} or smaller"
        )
    return {
        **base,
        "tier": tier,
        "minimum_model": minimum_model,
        "ollama_query_model": recommended_model,
        "meets_minimum": meets_minimum,
        "meets_recommended": meets_recommended,
        "reason": reason,
    }
