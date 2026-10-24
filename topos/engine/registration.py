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


# The answer protocol this node serves, 0 when answers are off: 1 wrote answers (1.5.0); 2 also carries the writer
# a policy's `writer_disclosure` releases (A2A-4 amendment 9). The control plane offers the sharer that choice only
# to a node advertising 2, because an older node refuses a policy naming the field. 3 (A2A-4 amendment 13) writes
# up to sixteen cited records in a with-sources body for an asker whose control plane says it accepts 3 on the ask
# frame, and eight for any other. 4 (answer protocol 4, 15 Oct 2026) writes up to thirty cited records and 32,000
# characters for an asker that says it accepts 4, and keeps 3's sixteen and 8,000 for one that accepts 3. Equals
# `permissions_v2.answer_protocol.ANSWER_PROTOCOL_VERSION` (pinned by test).
ANSWER_VERSION = 4


def _answer_version() -> int:
    try:
        from ..permissions_v2 import switches
        return ANSWER_VERSION if (switches.is_bound() and switches.on(switches.MESSAGE_SEARCH)
                                  and switches.on(switches.ANSWERS)) else 0
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


def _truth_version() -> int:
    from ..permissions_v2 import switches
    return 1 if switches.on(switches.MESSAGE_SEARCH) else 0


def _truth_interpret_version() -> int:
    """`permissions_v2_truth_interpret_version`: 1 when this node reads a free statement with its pinned local model
    (verify-claim effort high: knowledge search and the interpreter switch on, the shipped examples intact and the
    pinned model ready), else 0. The control plane forwards a statement its grammar refused only to a node that says 1;
    any other node gets the fast path only."""
    try:
        from ..permissions_v2.truth_statement import capability_version

        return int(capability_version())
    except Exception:  # noqa: BLE001 -- a node that cannot tell does not read statements
        return 0


def _fact_ontology_switches() -> int:
    """`permissions_v2_fact_ontology_switches`: 1, this node releases a fact only while the fact's ontology is
    switched on here (selective_facts.ONTOLOGY_SWITCHES_VERSION, where the rule is). It says what the code does, so
    it does not follow the facts switch: the catalog's kinds say whether facts are offered at all. 0 when the module
    cannot be read, which the control plane treats as "not said": it then offers no Facts for this node."""
    try:
        from ..permissions_v2.selective_facts import ONTOLOGY_SWITCHES_VERSION

        return int(ONTOLOGY_SWITCHES_VERSION)
    except Exception:  # noqa: BLE001 -- an unimportable module advertises no rule
        return 0


def _bind_version() -> int:
    """`permissions_v2_bind_version` (A2A-1 §3.5): 1, this node answers the control plane's bind; 0 if it cannot, and
    0 while its sharing kill switch is on, since every bind would be refused then (review N2 finding 7)."""
    try:
        from ..permissions_v2.bind_protocol import CAPABILITY_VERSION
        from ..permissions_v2.self_bind import binding_switched_off

        return 0 if binding_switched_off() else CAPABILITY_VERSION
    except Exception:  # noqa: BLE001 -- an unimportable bind module answers no binds
        return 0


def _knowledge_max_k() -> int:
    """`permissions_v2_knowledge_max_k` (MK165): the most a knowledge grant may sign per search on this node
    (knowledge_contract.KNOWLEDGE_MAX_K). The control plane compiles no share above it: a policy above a node's
    ceiling is refused at parse there, and the share would wait on activation. 0 when the module cannot be read,
    which the control plane treats as "not said" (its pre-MK165 ceiling of 20)."""
    try:
        from ..permissions_v2.knowledge_contract import KNOWLEDGE_MAX_K

        return int(KNOWLEDGE_MAX_K)
    except Exception:  # noqa: BLE001 -- an unimportable contract advertises no ceiling
        return 0


def _kept_answers() -> int:
    """`permissions_v2_kept_answers` (kept questions, lane OFF1N): 1, this node keeps a kept question's answer for the
    window its `kept_stamp` signs, and the fetch of a kept ask it could not serve then answers 429 `answer_deferred`,
    to be asked again. The control plane delivers a kept question only to a node that says so; to any other it is a
    live ask, kept ten minutes. It says what the code does, so it does not follow the answers switch
    (`permissions_v2_answer_version` says whether this node answers at all). 0 when the module cannot be read."""
    try:
        from ..permissions_v2.kept_stamp import CAPABILITY_VERSION

        return int(CAPABILITY_VERSION)
    except Exception:  # noqa: BLE001 -- an unimportable module keeps nothing
        return 0


def _away_config() -> int:
    """`permissions_v2_away_config` (kept questions Wave 2, lane OFF2N): 1, this node takes `put_away_config`, the
    owner's "While you're using Topos" and "Answer others only when …", and schedules by them live. The control plane
    sends them only to a node that says so. 0 when the module cannot be read."""
    try:
        from ..permissions_v2.away_config import CAPABILITY_VERSION

        return int(CAPABILITY_VERSION)
    except Exception:  # noqa: BLE001 -- an unimportable module takes nothing
        return 0


def _sharing_readiness_version() -> int:
    """`permissions_v2_sharing_readiness` (kept questions Wave 2, lane OFF2N): 1, this node sends `sharing_readiness`
    on its heartbeat and register and answers `get_sharing_readiness`; the control plane paces kept questions by it
    only for a node that says so. 0 when the module cannot be read."""
    try:
        from ..permissions_v2.sharing_readiness import CAPABILITY_VERSION

        return int(CAPABILITY_VERSION)
    except Exception:  # noqa: BLE001 -- an unimportable module reports nothing
        return 0


def sharing_readiness() -> Dict[str, Any] | None:
    """The readiness object both presence messages carry (contract §C), from memory; None when it cannot be built
    (the key is then left out, and the control plane treats the node as one without the capability)."""
    try:
        from ..permissions_v2.sharing_readiness import build

        return build()
    except Exception as exc:  # noqa: BLE001 -- readiness never fails a heartbeat
        logger.debug("sharing readiness not built: %s", type(exc).__name__)
        return None


def _with_readiness(payload: Dict[str, Any]) -> Dict[str, Any]:
    readiness = sharing_readiness()
    if readiness is not None:
        payload["sharing_readiness"] = readiness
    return payload


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
        "permissions_v2_truth_version": _truth_version(),
        "permissions_v2_truth_interpret_version": _truth_interpret_version(),
        "permissions_v2_answer_model": _answer_model_status(),
        # Facts by ontology: >= 1 says this node shares a fact only while its ontology is switched on here. The
        # control plane offers Facts, and forwards a fact, only for a node that says so (a node before the rule
        # does not check the owner's switches).
        "permissions_v2_fact_ontology_switches": _fact_ontology_switches(),
        # Binding for sharing (any-to-any A2A-1 §3.5): the control plane offers setup only to a node that says it
        # answers the bind, and it compares the key id with the one its registry holds. The id is a hint: it can
        # only ever ask the owner to confirm a new key, never change one.
        "permissions_v2_bind_version": _bind_version(),
        "permissions_v2_node_key_id": _node_key_id(),
        # MK165: the most a knowledge grant may sign per search here, so the control plane never compiles a share
        # this node would refuse at parse (its "Items per answer" choices stop at this number).
        "permissions_v2_knowledge_max_k": _knowledge_max_k(),
        # >= 1: a knowledge share here may opt into naming each AI-chat message's chat (`release_conversation_title`).
        # The control plane offers the choice only to a node that says so; a node before it refuses the key at parse.
        "permissions_v2_conversation_titles": 1,
        # Kept questions: >= 1 keeps a kept answer for its signed window and defers a kept ask it cannot serve now.
        "permissions_v2_kept_answers": _kept_answers(),
        # Kept questions, Wave 2: >= 1 takes the owner's away settings (`put_away_config`) and applies them live.
        "permissions_v2_away_config": _away_config(),
        # Kept questions, Wave 2: >= 1 sends `sharing_readiness` on the heartbeat and register and answers
        # `get_sharing_readiness` (whether this node has caught up, as counts; the control plane only, DR-37).
        "permissions_v2_sharing_readiness": _sharing_readiness_version(),
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
        "payload": _with_readiness({
            "occurred_at": now,
            "status": "connected",
            "transport_mode": resolve_transport_mode(),
            "capabilities": build_engine_capabilities(),
            "metadata": {
                "engine_mode": settings.engine_mode,
                "enable_llm": settings.enable_llm,
            },
        }),
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
        "payload": _with_readiness({
            "occurred_at": now,
            "status": "connected",
            "transport_mode": resolve_transport_mode(),
            "capabilities": build_engine_capabilities(),
            "metadata": {
                "engine_mode": settings.engine_mode,
                "enable_llm": settings.enable_llm,
                **memory_meta,
            },
        }),
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
