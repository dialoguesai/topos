from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from ..config.settings import settings


logger = logging.getLogger("topos.engine.registration")

CAPABILITIES_SCHEMA_VERSION = "v2"

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
        "ollama_pull_model",
        "ollama_pull_status",
        "ollama_install",
        "ollama_install_status",
    ],
}


def ollama_is_reachable() -> bool:
    """True when this machine's Ollama server actually answers."""
    from .backends.ollama import OllamaAdapter

    return OllamaAdapter().is_reachable()


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


def _search_batch_version() -> int:
    try:
        from ..permissions_v2.search_transport import batch_capability_version

        return batch_capability_version()
    except Exception:  # noqa: BLE001 -- an unimportable search module answers no batches
        return 0


def _answer_version() -> int:
    try:
        from ..permissions_v2 import switches
        return int(switches.is_bound() and switches.on(switches.MESSAGE_SEARCH)
                   and switches.on(switches.ANSWERS))
    except Exception:
        return 0


def _answer_model_status() -> str:
    if not _answer_version():
        return "missing"
    try:
        from ..permissions_v2.checking_model import status
        value = status()["status"]
        return "ready" if value == "ready" else "unsupported" if value == "unsupported" else "missing"
    except Exception:
        return "missing"


def _bind_version() -> int:
    """`permissions_v2_bind_version` (A2A-1 §3.5): 1, this node answers the control plane's bind; 0 if it cannot, and
    0 while its sharing kill switch is on, since every bind would be refused then (review N2 finding 7)."""
    try:
        from ..permissions_v2.bind_protocol import CAPABILITY_VERSION
        from ..permissions_v2.self_bind import binding_switched_off

        return 0 if binding_switched_off() else CAPABILITY_VERSION
    except Exception:  # noqa: BLE001 -- an unimportable bind module answers no binds
        return 0


def _node_key_id() -> Any:
    """`permissions_v2_node_key_id` (A2A-1 §3.5): the key id of this node's sharing config, or None. A hint, read
    from disk on every beat; it takes no lock and loads no runtime."""
    try:
        from ..permissions_v2.self_bind import node_key_id_hint

        return node_key_id_hint()
    except Exception:  # noqa: BLE001 -- a hint never fails a heartbeat
        return None


def build_engine_capabilities() -> Dict[str, Any]:
    runtime_profile = resolve_runtime_profile()
    providers: list[str] = []
    models: list[str] = []

    if settings.enable_llm:
        providers.append("openai")
        if settings.openai_model:
            models.append(settings.openai_model)

    # A configured base URL proves nothing: it defaults to localhost:11434, so
    # the old truthiness test registered every node ever started as
    # Ollama-capable, including machines that have never had it installed
    # (PLAN_LOCAL_MODEL_QUICKSTART §1.2). Only a server that answers counts.
    if settings.engine_ollama_base_url:
        try:
            if ollama_is_reachable():
                providers.append("ollama")
        except Exception as exc:  # noqa: BLE001 — a broken probe is not a capability
            logger.debug("ollama reachability probe failed: %s", exc)
    providers.append("huggingface")

    capability_tiers = ["tier.core", "tier.summary"]
    signal_jobs_available: list[str] = []
    try:
        from ..storage.adapters.factory import AdapterFactory

        bundle = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
        capability_tiers.extend(["tier.vector", "tier.graph"])
        from ..enrichment.jobs import SIGNAL_JOB_REGISTRY

        signal_jobs_available = sorted(SIGNAL_JOB_REGISTRY.keys())
    except Exception:
        pass

    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "providers": sorted(set(providers)),
        "models": sorted(set(models)),
        "supports_filtering": True,
        "supports_sanitization": True,
        "supports_enrichment": True,
        # Staged-deadline stream protocol (PLAN_HOME_CHAT_STREAMING_SLA §2):
        # version 1 = ack/heartbeat/thinking chunk kinds + llm_cancel. The
        # control plane keys cancel emission and frame translation on this,
        # not on version-string parsing.
        "llm_stream_protocol_version": 1,
        # Batched recipient search (OD-36): >= 1 lets the CP relay one batch frame instead of N
        # single ones. 0 whenever either search flag is off, so the CP never sends what we refuse.
        "permissions_v2_search_batch_version": _search_batch_version(),
        "permissions_v2_answer_version": _answer_version(),
        "permissions_v2_answer_model": _answer_model_status(),
        # Binding for sharing (any-to-any A2A-1 §3.5): the control plane offers setup only to a node that says it
        # answers the bind, and it compares the key id with the one its registry holds. The id is a hint: it can
        # only ever ask the owner to confirm a new key, never change one.
        "permissions_v2_bind_version": _bind_version(),
        "permissions_v2_node_key_id": _node_key_id(),
        "capability_tiers": capability_tiers,
        "signal_providers": [p for p in ("huggingface", "ollama") if p in providers],
        "signal_jobs_available": signal_jobs_available,
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
    """Presence, plus a FRESHLY probed capability set.

    Capabilities used to ride on registration alone. Ollama is installed and
    started by a human, often minutes after the node — so a node that came up
    first advertised no Ollama for the rest of its process life, and the owner
    who followed the quick-start saw nothing change (journey Branch C).
    Re-probing here is what lets the capability arrive late, and leave again.
    """
    now = datetime.now(timezone.utc).isoformat()
    memory_meta: Dict[str, Any] = {}
    try:
        from .memory_utils import get_process_rss_mb
        from .model_cache import get_model_cache

        cache = get_model_cache()
        memory_meta = {
            "rss_mb": get_process_rss_mb(),
            "resident_model_slots": cache.resident_slots(),
            "max_resident_models": cache.max_resident,
            "model_evictions_total": cache.evictions_total,
        }
    except Exception:
        pass
    return {
        "id": str(uuid4()),
        "type": "engine_heartbeat",
        "payload": {
            "occurred_at": now,
            "status": "connected",
            "transport_mode": resolve_transport_mode(),
            "capabilities": build_engine_capabilities(),
            "metadata": {
                "engine_mode": settings.engine_mode,
                "enable_llm": settings.enable_llm,
                **memory_meta,
            },
        },
    }


async def build_engine_register_message_async() -> Dict[str, Any]:
    """`build_engine_register_message` off the event loop.

    Capability building probes Ollama over a blocking socket with a timeout. The
    presence loop is asyncio and shares its thread with every other engine task,
    so a hung Ollama would stall them all for the duration.
    """
    return await asyncio.to_thread(build_engine_register_message)


async def build_engine_heartbeat_message_async() -> Dict[str, Any]:
    """`build_engine_heartbeat_message` off the event loop — see above.

    This one matters more: it now re-probes on every beat, so the blocking call
    would land on the loop every 30 seconds rather than once.
    """
    return await asyncio.to_thread(build_engine_heartbeat_message)
