from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from ..config.settings import settings


CAPABILITIES_SCHEMA_VERSION = "v1"

RUNTIME_PROFILE_OPERATIONS: dict[str, list[str]] = {
    "basic_hosted": ["healthcheck", "sanitization.run", "filter_lab.list_job_groups"],
    "upgraded_hosted": [
        "healthcheck",
        "sanitization.run",
        "filter_lab.list_job_groups",
        "filter_lab.run",
        "filter_lab.create_job_group",
    ],
    "local_engine": [
        "healthcheck",
        "sanitization.run",
        "filter_lab.list_job_groups",
        "filter_lab.run",
        "filter_lab.create_job_group",
        "llm_generation",
        "ollama_list_models",
    ],
}


def resolve_runtime_profile() -> str:
    raw = str(getattr(settings, "topos_compute_profile", "basic_hosted") or "basic_hosted").strip().lower()
    aliases = {
        "basic": "basic_hosted",
        "hosted_basic": "basic_hosted",
        "pro": "upgraded_hosted",
        "hosted_pro": "upgraded_hosted",
        "upgraded": "upgraded_hosted",
        "local": "local_engine",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in RUNTIME_PROFILE_OPERATIONS:
        return "basic_hosted"
    return normalized


def build_engine_capabilities() -> Dict[str, Any]:
    runtime_profile = resolve_runtime_profile()
    providers: list[str] = []
    models: list[str] = []

    if settings.enable_llm:
        providers.append("openai")
        if settings.openai_model:
            models.append(settings.openai_model)

    if settings.engine_ollama_base_url:
        providers.append("ollama")

    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "providers": sorted(set(providers)),
        "models": sorted(set(models)),
        "supports_filtering": True,
        "supports_sanitization": True,
        "supports_enrichment": True,
        "operations": list(RUNTIME_PROFILE_OPERATIONS.get(runtime_profile, [])),
        "runtime_profile": {
            "id": runtime_profile,
            "allowed_operations": list(RUNTIME_PROFILE_OPERATIONS.get(runtime_profile, [])),
            "deployment_mode": "local" if runtime_profile == "local_engine" else "hosted",
            "pricing_tier": "pro" if runtime_profile == "upgraded_hosted" else ("local" if runtime_profile == "local_engine" else "basic"),
        },
        "limits": {
            "sanitization_ollama_max_input_chars": settings.sanitization_ollama_max_input_chars,
            "request_timeout_seconds": settings.request_timeout_seconds,
        },
        "transport": {
            "mode": resolve_transport_mode(),
            "control_plane_url_configured": bool(settings.topos_control_plane_url),
        },
    }


def resolve_transport_mode() -> str:
    mode = str(getattr(settings, "engine_transport_mode", "ws") or "ws").strip().lower()
    if mode not in {"ws", "endpoint"}:
        return "ws"
    return mode


def build_engine_register_message() -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid4()),
        "type": "engine_register",
        "payload": {
            "occurred_at": now,
            "status": "connected",
            "transport_mode": resolve_transport_mode(),
            "capabilities": build_engine_capabilities(),
            "metadata": {
                "engine_mode": settings.engine_mode,
                "enable_llm": settings.enable_llm,
            },
        },
    }


def build_engine_heartbeat_message() -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid4()),
        "type": "engine_heartbeat",
        "payload": {
            "occurred_at": now,
            "status": "connected",
            "transport_mode": resolve_transport_mode(),
            "metadata": {
                "engine_mode": settings.engine_mode,
                "enable_llm": settings.enable_llm,
            },
        },
    }
